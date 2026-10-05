class TargetPosition:
    """Tiny example, not an alpha model. State comes only from Rust snapshots."""
    def __init__(self, target=3, exit_bid=None):
        self.target = target
        self.exit_bid = exit_bid

    def on_quote(self, state):
        if state["health"] != "Healthy" or state["killed"] or not state["quote"]:
            return None
        if any(o["lifecycle"] not in ("Filled", "Canceled", "Rejected") or o["uncertain"]
               for o in state["orders"].values()):
            return None
        bid, ask, _ = state["quote"]
        target = 0 if self.exit_bid is not None and bid >= self.exit_bid else self.target
        delta = target - state["position"]
        if not delta:
            return None
        return {"id": max(map(int, state["orders"]), default=0) + 1,
                "side": "Buy" if delta > 0 else "Sell", "qty": abs(delta),
                "limit": ask if delta > 0 else bid, "based_on_seq": state["seq"],
                "valid_until": state["now"] + 1000}

