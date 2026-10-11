import * as cdk from "aws-cdk-lib";
import * as budgets from "aws-cdk-lib/aws-budgets";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as s3 from "aws-cdk-lib/aws-s3";
import { Construct } from "constructs";

/** Graviton sizes; prices are Tokyo on-demand, verified 2026-10-11 via the Price List API. */
export const INSTANCE_SIZES = {
  micro: { type: "t4g.micro", hourlyUsd: 0.0108, memoryGiB: 1 },
  small: { type: "t4g.small", hourlyUsd: 0.0216, memoryGiB: 2 },
} as const;
export type InstanceSize = keyof typeof INSTANCE_SIZES;

export const PRICES = {
  publicIpv4HourlyUsd: 0.005,
  gp3GiBMonthUsd: 0.096,
  hoursPerMonth: 730,
} as const;

export interface MiniNautilusHostStackProps extends cdk.StackProps {
  readonly instanceSize: InstanceSize;
  /** Root gp3 volume; journals, the Rust toolchain and a swap file live here. */
  readonly volumeGiB: number;
  /** Monthly cost budget in USD (an alert, never a hard cap). */
  readonly budgetUsd: number;
  /** Optional address for budget alerts. */
  readonly alertEmail?: string;
}

/** Fixed monthly cost of the always-on parts (excludes data transfer, S3 and tax). */
export function fixedMonthlyUsd(size: InstanceSize, volumeGiB: number): number {
  const h = PRICES.hoursPerMonth;
  return (
    INSTANCE_SIZES[size].hourlyUsd * h +
    PRICES.publicIpv4HourlyUsd * h +
    PRICES.gp3GiBMonthUsd * volumeGiB
  );
}

/**
 * One small always-on host for the MiniNautilus engine.
 *
 * Cost shape (see infra/README.md):
 * - Public subnet + Elastic IP: a fixed outbound address for the Binance API
 *   whitelist with no NAT Gateway (no hourly NAT charge, no per-GB processing).
 * - No inbound rules; operators use SSM Session Manager (no SSH, no bastion).
 * - S3 traffic goes through a free gateway endpoint; no interface endpoints,
 *   load balancers or CloudWatch Logs agents.
 * - Standard (not unlimited) CPU credits: sustained CPU is throttled, never billed.
 */
export class MiniNautilusHostStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: MiniNautilusHostStackProps) {
    super(scope, id, props);
    const size = INSTANCE_SIZES[props.instanceSize];

    const vpc = new ec2.Vpc(this, "Vpc", {
      ipAddresses: ec2.IpAddresses.cidr("10.42.0.0/24"),
      maxAzs: 1,
      natGateways: 0,
      subnetConfiguration: [
        {
          name: "public",
          subnetType: ec2.SubnetType.PUBLIC,
          cidrMask: 26,
          // No auto-assigned IPv4 (each is billed); the Elastic IP is the only one.
          mapPublicIpOnLaunch: false,
        },
      ],
      gatewayEndpoints: {
        S3: { service: ec2.GatewayVpcEndpointAwsService.S3 },
      },
    });

    // Journals, reports and build artifacts; stays in-region (gateway endpoint).
    const bucket = new s3.Bucket(this, "Artifacts", {
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: false,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      lifecycleRules: [
        { prefix: "runs/", expiration: cdk.Duration.days(90) },
        { abortIncompleteMultipartUploadAfter: cdk.Duration.days(1) },
      ],
    });

    const securityGroup = new ec2.SecurityGroup(this, "HostSg", {
      vpc,
      description: "MiniNautilus host: no inbound; outbound HTTPS/HTTP only",
      allowAllOutbound: false,
    });
    securityGroup.addEgressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(443),
      "Binance, SSM, S3, packages",
    );
    securityGroup.addEgressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(80),
      "OS package mirrors",
    );

    const role = new iam.Role(this, "HostRole", {
      assumedBy: new iam.ServicePrincipal("ec2.amazonaws.com"),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          "AmazonSSMManagedInstanceCore",
        ),
      ],
    });
    bucket.grantReadWrite(role);

    const userData = ec2.UserData.forLinux();
    userData.addCommands(
      "set -euxo pipefail",
      // Swap lets a 1 GiB host build the Rust release binary (fat LTO).
      "if [ ! -f /swapfile ]; then fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile; fi",
      "swapon /swapfile || true",
      "grep -q /swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab",
      "dnf install -y -q git gcc python3.11 python3.11-pip",
      "id trader || useradd --create-home trader",
      `echo 'ARTIFACT_BUCKET=${bucket.bucketName}' > /etc/mininautilus.env`,
      "echo bootstrap-complete > /var/log/mininautilus-bootstrap.done",
    );

    const instance = new ec2.Instance(this, "Host", {
      vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      instanceType: new ec2.InstanceType(size.type),
      machineImage: ec2.MachineImage.latestAmazonLinux2023({
        cpuType: ec2.AmazonLinuxCpuType.ARM_64,
      }),
      securityGroup,
      role,
      userData,
      requireImdsv2: true, // cloud-init reads user data through IMDSv2
      associatePublicIpAddress: false, // the Elastic IP below is the only public address
      creditSpecification: ec2.CpuCredits.STANDARD,
      blockDevices: [
        {
          deviceName: "/dev/xvda",
          volume: ec2.BlockDeviceVolume.ebs(props.volumeGiB, {
            volumeType: ec2.EbsDeviceVolumeType.GP3,
            encrypted: true,
            deleteOnTermination: true,
          }),
        },
      ],
    });

    const eip = new ec2.CfnEIP(this, "WhitelistIp", { domain: "vpc" });
    new ec2.CfnEIPAssociation(this, "WhitelistIpBinding", {
      allocationId: eip.attrAllocationId,
      instanceId: instance.instanceId,
    });

    new budgets.CfnBudget(this, "MonthlyBudget", {
      budget: {
        budgetName: `${this.stackName}-monthly`,
        budgetType: "COST",
        timeUnit: "MONTHLY",
        budgetLimit: { amount: props.budgetUsd, unit: "USD" },
      },
      notificationsWithSubscribers: props.alertEmail
        ? [80, 100].map((threshold) => ({
            notification: {
              notificationType: "FORECASTED",
              comparisonOperator: "GREATER_THAN",
              threshold,
              thresholdType: "PERCENTAGE",
            },
            subscribers: [
              { subscriptionType: "EMAIL", address: props.alertEmail! },
            ],
          }))
        : [],
    });

    cdk.Tags.of(this).add("project", "mininautilus");
    new cdk.CfnOutput(this, "WhitelistIPv4", {
      value: eip.attrPublicIp,
      description: "Add to the Binance API key IP whitelist",
    });
    new cdk.CfnOutput(this, "InstanceId", { value: instance.instanceId });
    new cdk.CfnOutput(this, "ArtifactBucket", { value: bucket.bucketName });
    new cdk.CfnOutput(this, "FixedMonthlyUsd", {
      value: fixedMonthlyUsd(props.instanceSize, props.volumeGiB).toFixed(2),
      description:
        "Instance + public IPv4 + gp3 at verified Tokyo prices; excludes transfer/S3/tax",
    });
    new cdk.CfnOutput(this, "SessionCommand", {
      value: `aws ssm start-session --region ${this.region} --target ${instance.instanceId}`,
    });
  }
}
