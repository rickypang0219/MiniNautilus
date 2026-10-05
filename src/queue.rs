//! Experimental bounded SPSC queue. Exactly one producer and one consumer.
//! Not yet used for financial state: the deterministic core is the reference model.
use std::{
    cell::{Cell, UnsafeCell},
    marker::PhantomData,
    mem::MaybeUninit,
    sync::{
        Arc,
        atomic::{
            AtomicBool, AtomicUsize,
            Ordering::{Acquire, Relaxed, Release},
        },
    },
};

#[repr(align(128))]
struct Indices([AtomicUsize; 32]);
struct Inner<T> {
    slots: Box<[UnsafeCell<MaybeUninit<T>>]>,
    indices: Indices,
    tail_slot: usize,
    producer_alive: AtomicBool,
    consumer_alive: AtomicBool,
}
// SAFETY: Only the unique producer writes a slot, after acquiring the consumer's
// head publication. Only the unique consumer reads it, after acquiring tail.
// These operations never overlap on an initialized slot. T crosses threads, so
// Send is required; T need not be Sync because no shared &T is ever exposed.
unsafe impl<T: Send> Sync for Inner<T> {}

pub struct Producer<T> {
    inner: Arc<Inner<T>>,
    _not_sync: PhantomData<Cell<()>>,
}
pub struct Consumer<T> {
    inner: Arc<Inner<T>>,
    _not_sync: PhantomData<Cell<()>>,
}
#[derive(Debug, PartialEq, Eq)]
pub enum PushError<T> {
    Full(T),
    Closed(T),
}
#[derive(Debug, PartialEq, Eq)]
pub enum PopError {
    Empty,
    Closed,
}

/// Power-of-two capacity ensures wraparound indexing is valid at usize overflow.
/// padded=false intentionally puts head/tail together for the false-sharing lab.
pub fn channel<T>(capacity: usize, padded: bool) -> (Producer<T>, Consumer<T>) {
    assert!(capacity.is_power_of_two() && capacity <= isize::MAX as usize);
    let inner = Arc::new(Inner {
        slots: (0..capacity)
            .map(|_| UnsafeCell::new(MaybeUninit::uninit()))
            .collect(),
        indices: Indices(std::array::from_fn(|_| AtomicUsize::new(0))),
        tail_slot: if padded { 16 } else { 1 },
        producer_alive: AtomicBool::new(true),
        consumer_alive: AtomicBool::new(true),
    });
    (
        Producer {
            inner: inner.clone(),
            _not_sync: PhantomData,
        },
        Consumer {
            inner,
            _not_sync: PhantomData,
        },
    )
}

impl<T> Producer<T> {
    /// Producer-side occupancy sample; consumer may advance immediately afterward.
    pub fn len(&self) -> usize {
        let q = &self.inner;
        let tail = q.indices.0[q.tail_slot].load(Relaxed);
        let head = q.indices.0[0].load(Acquire);
        tail.wrapping_sub(head)
    }
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
    pub fn try_push(&mut self, value: T) -> Result<(), PushError<T>> {
        let q = &self.inner;
        if !q.consumer_alive.load(Acquire) {
            return Err(PushError::Closed(value));
        }
        let tail = q.indices.0[q.tail_slot].load(Relaxed);
        let head = q.indices.0[0].load(Acquire);
        if tail.wrapping_sub(head) == q.slots.len() {
            return Err(PushError::Full(value));
        }
        // SAFETY: acquired head grants exclusive access to this vacant slot.
        unsafe {
            (*q.slots[tail & (q.slots.len() - 1)].get()).write(value);
        }
        // Publishes the initialized payload to the consumer.
        q.indices.0[q.tail_slot].store(tail.wrapping_add(1), Release);
        Ok(())
    }
}
impl<T> Consumer<T> {
    pub fn try_pop(&mut self) -> Result<T, PopError> {
        let q = &self.inner;
        let head = q.indices.0[0].load(Relaxed);
        let mut tail = q.indices.0[q.tail_slot].load(Acquire);
        if head == tail {
            if q.producer_alive.load(Acquire) {
                return Err(PopError::Empty);
            }
            // Producer may have published its final item between the two loads.
            tail = q.indices.0[q.tail_slot].load(Acquire);
            if head == tail {
                return Err(PopError::Closed);
            }
        }
        // SAFETY: acquired tail proves initialization; this is the sole consumer.
        let value = unsafe { (*q.slots[head & (q.slots.len() - 1)].get()).assume_init_read() };
        // Publishes that the producer may reuse the slot after its next acquire.
        q.indices.0[0].store(head.wrapping_add(1), Release);
        Ok(value)
    }
}
impl<T> Drop for Producer<T> {
    fn drop(&mut self) {
        self.inner.producer_alive.store(false, Release);
    }
}
impl<T> Drop for Consumer<T> {
    fn drop(&mut self) {
        self.inner.consumer_alive.store(false, Release);
    }
}
impl<T> Drop for Inner<T> {
    fn drop(&mut self) {
        // Last Arc is gone; no endpoint can access the slots now.
        let head = *self.indices.0[0].get_mut();
        let tail = *self.indices.0[self.tail_slot].get_mut();
        for offset in 0..tail.wrapping_sub(head) {
            let slot = head.wrapping_add(offset) & (self.slots.len() - 1);
            // SAFETY: exactly these slots still contain initialized, unread values.
            unsafe {
                self.slots[slot].get_mut().assume_init_drop();
            }
        }
    }
}

#[derive(Clone, Copy, Debug)]
pub enum Wait {
    Spin,
    Yield,
    Hybrid,
}
impl Wait {
    pub fn idle(self, attempts: usize) {
        match self {
            Self::Spin => std::hint::spin_loop(),
            Self::Yield => std::thread::yield_now(),
            Self::Hybrid if attempts < 100 => std::hint::spin_loop(),
            Self::Hybrid => std::thread::park_timeout(std::time::Duration::from_micros(50)),
        }
    }
}

#[cfg(target_os = "linux")]
pub fn pin_current(cpu: usize) -> std::io::Result<()> {
    if cpu >= libc::CPU_SETSIZE as usize {
        return Err(std::io::Error::other("CPU index out of range"));
    }
    // SAFETY: set is fully initialized and passed with its actual size; pid=0
    // targets the calling thread. The kernel validates allowed CPU masks.
    unsafe {
        let mut set: libc::cpu_set_t = std::mem::zeroed();
        libc::CPU_ZERO(&mut set);
        libc::CPU_SET(cpu, &mut set);
        if libc::sched_setaffinity(0, std::mem::size_of_val(&set), &set) != 0 {
            return Err(std::io::Error::last_os_error());
        }
    }
    Ok(())
}
#[cfg(not(target_os = "linux"))]
pub fn pin_current(_cpu: usize) -> std::io::Result<()> {
    Err(std::io::Error::new(
        std::io::ErrorKind::Unsupported,
        "strict CPU pinning experiment requires Linux",
    ))
}

/// Compact derived index for active/dirty slots; never authoritative order state.
#[derive(Default)]
pub struct Bitmap {
    words: Vec<u64>,
}
impl Bitmap {
    pub fn with_capacity(bits: usize) -> Self {
        Self {
            words: vec![0; bits.div_ceil(64)],
        }
    }
    pub fn set(&mut self, bit: usize, value: bool) {
        let mask = 1 << (bit % 64);
        if value {
            self.words[bit / 64] |= mask;
        } else {
            self.words[bit / 64] &= !mask;
        }
    }
    pub fn contains(&self, bit: usize) -> bool {
        self.words
            .get(bit / 64)
            .is_some_and(|w| w & (1 << (bit % 64)) != 0)
    }
    pub fn ones(&self) -> impl Iterator<Item = usize> + '_ {
        self.words.iter().enumerate().flat_map(|(index, word)| {
            let mut remaining = *word;
            std::iter::from_fn(move || {
                if remaining == 0 {
                    return None;
                }
                let bit = remaining.trailing_zeros() as usize;
                remaining &= remaining - 1;
                Some(index * 64 + bit)
            })
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn index_wraparound_preserves_slots() {
        let (mut tx, mut rx) = channel(4, true);
        // Empty ring immediately before monotonic counters wrap around usize.
        tx.inner.indices.0[0].store(usize::MAX - 1, Relaxed);
        tx.inner.indices.0[tx.inner.tail_slot].store(usize::MAX - 1, Relaxed);
        for n in 0..4 {
            tx.try_push(n).unwrap();
        }
        assert_eq!(tx.try_push(4), Err(PushError::Full(4)));
        for n in 0..4 {
            assert_eq!(rx.try_pop(), Ok(n));
        }
        assert_eq!(rx.try_pop(), Err(PopError::Empty));
    }
}
