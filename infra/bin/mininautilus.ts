#!/usr/bin/env node
import "source-map-support/register";
import * as cdk from "aws-cdk-lib";
import {
  INSTANCE_SIZES,
  InstanceSize,
  MiniNautilusHostStack,
  fixedMonthlyUsd,
} from "../lib/mininautilus-host-stack";

const app = new cdk.App();
const instanceSize = (app.node.tryGetContext("instanceSize") ??
  "micro") as InstanceSize;
if (!(instanceSize in INSTANCE_SIZES)) {
  throw new Error(
    `instanceSize must be one of ${Object.keys(INSTANCE_SIZES).join(", ")}`,
  );
}
const volumeGiB = Number(app.node.tryGetContext("volumeGiB") ?? 12);
const budgetUsd = Number(app.node.tryGetContext("budgetUsd") ?? 20);
const fixed = fixedMonthlyUsd(instanceSize, volumeGiB);
if (fixed > budgetUsd) {
  throw new Error(
    `fixed cost ${fixed.toFixed(2)} USD/month exceeds budget ${budgetUsd}`,
  );
}

new MiniNautilusHostStack(app, "MiniNautilusHost", {
  // Default region ap-northeast-1 (CLAUDE.md); account from the deploying credentials.
  env: { region: "ap-northeast-1", account: process.env.CDK_DEFAULT_ACCOUNT },
  instanceSize,
  volumeGiB,
  budgetUsd,
  alertEmail: app.node.tryGetContext("alertEmail"),
  description: "MiniNautilus trading engine host (single EC2, no NAT Gateway)",
});
