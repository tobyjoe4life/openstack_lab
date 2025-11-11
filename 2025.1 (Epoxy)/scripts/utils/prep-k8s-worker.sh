#!/usr/bin/env bash
set -euxo pipefail

# This script is for educational purposes only. It is not intended for production use.
# This scripts outputs a Kubernetes worker node user data YAML for cloud-init.
# The script requires one argument: the filename containing Kubernetes cluster join command fetched from master.

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <join-command-file>"
  exit 1
fi

JOIN_CMD=$(cat "$1")

# Output the cloud-init user data for a Kubernetes worker node
cat <<EOF
#cloud-config
#
# --- Kubernetes worker bootstrap for image "ubuntu24.04-k8s-1.33" ---
#

write_files:
  - path: /usr/local/bin/join-k8s-worker.sh
    owner: root:root
    permissions: "0755"
    content: |
      #!/usr/bin/env bash
      set -euxo pipefail

      # ------------------------------------------------------------------
      # 1.  Join the cluster (idempotent)
      # ------------------------------------------------------------------
      if ! systemctl is-active --quiet kubelet; then
        $JOIN_CMD --cri-socket /run/containerd/containerd.sock
      fi

      # ------------------------------------------------------------------
      # 2.  (Optional)  Show that the node joined successfully
      # ------------------------------------------------------------------
      echo "====> kubeadm join completed; kubelet should be starting."
      echo "====> First 'Ready' status may take ~30 s while Calico pulls its images."

runcmd:
  - /usr/local/bin/join-k8s-worker.sh
EOF