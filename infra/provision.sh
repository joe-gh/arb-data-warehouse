#!/usr/bin/env bash
#
# Provision the FDM4 data-warehouse box (proof-of-concept).
#
#   - r7g.xlarge (4 vCPU / 32 GB, Graviton), Ubuntu 24.04 LTS arm64
#   - single 200 GB gp3 root volume (default IOPS/throughput)
#   - Elastic IP (stable egress -> later this is the IP FDM4 whitelists)
#   - software (Postgres 18 + pgvector + PgBouncer + Java + Python) via cloud-init.sh
#
# Read-only-safe to read; it CREATES billable resources when run. Review the
# CONFIG block, then run:  bash db-test/infra/provision.sh
#
set -euo pipefail

############################  CONFIG  ############################
REGION="us-east-2"
NAME="fdm4-warehouse"
AMI="ami-0ecd65aaebb33ebda"          # Ubuntu 24.04 LTS arm64 (us-east-2, latest as of 2026-06-16)
INSTANCE_TYPE="r7g.xlarge"
VOLUME_GB="200"
VPC_ID="vpc-4237ba2a"                # the single default VPC
SUBNET_ID="subnet-0b84b346"          # us-east-2c, public
KEY_NAME="newJD"                     # <-- CONFIRM: which key pair you hold the private key for

# Who may reach the box:
SSH_CIDR="76.226.179.118/32"         # <-- CONFIRM: your admin IP for SSH (22)
PG_CIDR="172.31.0.0/16"              # PgBouncer (6432) reachable VPC-internal only (Woo + Insights inside the VPC)
##################################################################

CLOUD_INIT="$(dirname "$0")/cloud-init.sh"

echo "==> Creating security group"
SG_ID=$(aws ec2 create-security-group \
  --region "$REGION" --vpc-id "$VPC_ID" \
  --group-name "${NAME}-sg" \
  --description "FDM4 warehouse: SSH (admin) + PgBouncer (VPC)" \
  --query 'GroupId' --output text)
echo "    SG: $SG_ID"

aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$SG_ID" \
  --ip-permissions "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=${SSH_CIDR},Description=admin-ssh}]" >/dev/null
aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$SG_ID" \
  --ip-permissions "IpProtocol=tcp,FromPort=6432,ToPort=6432,IpRanges=[{CidrIp=${PG_CIDR},Description=pgbouncer-vpc}]" >/dev/null
echo "    ingress: 22 from ${SSH_CIDR}, 6432 from ${PG_CIDR}"

echo "==> Launching instance"
INSTANCE_ID=$(aws ec2 run-instances --region "$REGION" \
  --image-id "$AMI" --instance-type "$INSTANCE_TYPE" \
  --key-name "$KEY_NAME" --subnet-id "$SUBNET_ID" --security-group-ids "$SG_ID" \
  --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=${VOLUME_GB},VolumeType=gp3,DeleteOnTermination=true}" \
  --metadata-options "HttpTokens=required" \
  --user-data "file://${CLOUD_INIT}" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=${NAME}},{Key=role,Value=data-warehouse},{Key=backup,Value=daily}]" \
  --query 'Instances[0].InstanceId' --output text)
echo "    instance: $INSTANCE_ID"

echo "==> Waiting for running state"
aws ec2 wait instance-running --region "$REGION" --instance-ids "$INSTANCE_ID"

echo "==> Allocating + associating Elastic IP"
ALLOC_ID=$(aws ec2 allocate-address --region "$REGION" --domain vpc \
  --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=${NAME}}]" \
  --query 'AllocationId' --output text)
aws ec2 associate-address --region "$REGION" --instance-id "$INSTANCE_ID" --allocation-id "$ALLOC_ID" >/dev/null
EIP=$(aws ec2 describe-addresses --region "$REGION" --allocation-ids "$ALLOC_ID" --query 'Addresses[0].PublicIp' --output text)

cat <<EOF

================================================================
  ${NAME} provisioned
  Instance : ${INSTANCE_ID}  (${INSTANCE_TYPE})
  Elastic IP: ${EIP}     <-- the IP to give FDM4 for whitelisting later
  SG       : ${SG_ID}
  SSH      : ssh -i ~/.ssh/${KEY_NAME}.pem ubuntu@${EIP}
  Postgres : connect to ${EIP}:6432 (PgBouncer) from inside the VPC
  Creds    : on the box at /root/arb_warehouse_credentials.txt (sudo)
  cloud-init log: /var/log/cloud-init-output.log (give it ~3-5 min)
================================================================
EOF
