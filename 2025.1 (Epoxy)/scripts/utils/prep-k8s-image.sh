#!/usr/bin/env bash
set -euo pipefail
set -x

# This script is for educational purposes only. It is not intended for production use.
# This script installs Kubernetes packages and prepares an Instance for Image creation from snapshot.

export DEBIAN_FRONTEND=noninteractive

### 1 OS update & kernel tweaks
apt-get update && apt-get dist-upgrade -y

cat >/etc/modules-load.d/k8s.conf <<'EOF'
overlay
br_netfilter
EOF
modprobe overlay br_netfilter

cat >/etc/sysctl.d/99-kubernetes-cri.conf <<'EOF'
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF
sysctl --system
swapoff -a && sed -i '/ swap / s/^/#/' /etc/fstab

### 2 containerd (systemd‑cgroup)
apt-get install -y containerd
mkdir -p /etc/containerd
containerd config default >/etc/containerd/config.toml
sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
systemctl restart containerd && systemctl enable containerd

### 3 Kubernetes 1.33 tools
apt-get install -y ca-certificates curl gpg apt-transport-https
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.33/deb/Release.key | gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.33/deb/ /' >/etc/apt/sources.list.d/kubernetes.list
apt-get update
apt-get install -y kubelet kubeadm kubectl
apt-mark hold kubelet kubeadm kubectl   # freeze until you decide to upgrade

### 4 Clean and power off
kubeadm reset --cri-socket /run/containerd/containerd.sock --force
cloud-init clean --logs
truncate -s 0 /etc/machine-id
apt-get clean
history -c