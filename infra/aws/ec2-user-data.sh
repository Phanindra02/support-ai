#!/bin/bash
# Runs once, as root, the first time the server boots: EC2 hands it to cloud-init as "user
# data". It installs Docker and readies the server to pull the API's image from ECR.
# What it printed is in /var/log/cloud-init-output.log on the server.
set -euo pipefail

# Docker's own apt repository, for the compose plugin, which Ubuntu's docker.io package lacks.
apt-get update
apt-get install -y ca-certificates curl git amazon-ecr-credential-helper
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc]" \
  "https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Installing Docker during the first boot leaves it stopped: apt reloads systemd's units
# while docker.socket is starting, the socket loses its file descriptor, and dockerd then has
# nothing to listen on. Restarting both once the install is done fixes it.
systemctl reset-failed docker.socket docker.service || true
systemctl restart docker.socket docker.service

# Lets the ubuntu user run docker without sudo.
usermod -aG docker ubuntu

# docker pull asks this helper to log in to ECR, and the helper uses the instance's IAM role.
# There's no registry password to store, and nothing to renew (an ECR login lasts 12 hours).
# The registry's name holds the account number, which the instance metadata service knows.
TOKEN=$(curl -sfX PUT http://169.254.169.254/latest/api/token \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
ACCOUNT_ID=$(curl -sfH "X-aws-ec2-metadata-token: ${TOKEN}" \
  http://169.254.169.254/latest/dynamic/instance-identity/document \
  | python3 -c "import json, sys; print(json.load(sys.stdin)['accountId'])")
mkdir -p /home/ubuntu/.docker
echo "{\"credHelpers\": {\"${ACCOUNT_ID}.dkr.ecr.us-east-2.amazonaws.com\": \"ecr-login\"}}" \
  > /home/ubuntu/.docker/config.json
chown -R ubuntu:ubuntu /home/ubuntu/.docker

# 2 GB of swap on disk, so a burst of memory use slows the server down instead of making the
# kernel kill a container. The instance has 2 GB of memory.
fallocate -l 2G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo "/swapfile none swap sw 0 0" >> /etc/fstab
