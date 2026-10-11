import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import {
  MiniNautilusHostStack,
  fixedMonthlyUsd,
} from "../lib/mininautilus-host-stack";

function synth(instanceSize: "micro" | "small" = "micro", alertEmail?: string) {
  const app = new cdk.App();
  const stack = new MiniNautilusHostStack(app, "Test", {
    env: { region: "ap-northeast-1", account: "123456789012" },
    instanceSize,
    volumeGiB: 12,
    budgetUsd: 20,
    alertEmail,
  });
  return Template.fromStack(stack);
}

describe("cost guards", () => {
  const t = synth();

  test("no NAT gateway, no interface endpoints, no load balancer", () => {
    t.resourceCountIs("AWS::EC2::NatGateway", 0);
    t.resourceCountIs("AWS::ElasticLoadBalancingV2::LoadBalancer", 0);
    const endpoints = t.findResources("AWS::EC2::VPCEndpoint");
    for (const e of Object.values(endpoints)) {
      expect(e.Properties.VpcEndpointType ?? "Gateway").toBe("Gateway");
    }
    expect(Object.keys(endpoints)).toHaveLength(1);
  });

  test("exactly one public IPv4 (the Elastic IP)", () => {
    t.resourceCountIs("AWS::EC2::EIP", 1);
    t.hasResourceProperties("AWS::EC2::Subnet", { MapPublicIpOnLaunch: false });
    t.hasResourceProperties("AWS::EC2::Instance", {
      InstanceType: "t4g.micro",
      CreditSpecification: { CPUCredits: "standard" },
      BlockDeviceMappings: [
        Match.objectLike({
          Ebs: Match.objectLike({
            VolumeType: "gp3",
            VolumeSize: 12,
            Encrypted: true,
          }),
        }),
      ],
    });
  });

  test("IMDSv2 required so user data still runs", () => {
    t.hasResourceProperties("AWS::EC2::LaunchTemplate", {
      LaunchTemplateData: { MetadataOptions: { HttpTokens: "required" } },
    });
  });

  test("no inbound rules", () => {
    for (const sg of Object.values(
      t.findResources("AWS::EC2::SecurityGroup"),
    )) {
      expect(sg.Properties.SecurityGroupIngress ?? []).toHaveLength(0);
    }
  });

  test("fixed monthly cost stays under budget", () => {
    expect(fixedMonthlyUsd("micro", 12)).toBeCloseTo(12.68, 1);
    expect(fixedMonthlyUsd("small", 12)).toBeLessThan(21);
    t.hasResourceProperties("AWS::Budgets::Budget", {
      Budget: Match.objectLike({ BudgetLimit: { Amount: 20, Unit: "USD" } }),
    });
  });
});

test("host reads only the Testnet key prefix", () => {
  synth().hasResourceProperties("AWS::IAM::Policy", {
    PolicyDocument: Match.objectLike({
      Statement: Match.arrayWith([
        Match.objectLike({
          Action: ["ssm:GetParameter", "ssm:GetParameters"],
          Resource: Match.stringLikeRegexp(
            "parameter/mininautilus/testnet/\\*$",
          ),
        }),
      ]),
    }),
  });
});

test("budget alert subscribers only when an email is given", () => {
  synth("micro", "ops@example.com").hasResourceProperties(
    "AWS::Budgets::Budget",
    {
      NotificationsWithSubscribers: Match.arrayWith([
        Match.objectLike({
          Subscribers: [
            { SubscriptionType: "EMAIL", Address: "ops@example.com" },
          ],
        }),
      ]),
    },
  );
});
