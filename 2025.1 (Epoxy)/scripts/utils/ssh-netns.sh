#!/usr/bin/env bash
set -euo pipefail

# This script is for educational purposes only. It is not intended for production use.
# It allows you to SSH into an OpenStack instance using the network namespace of its router.

# Usage check
if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "Usage: $0 <instance-name> <guest-username> [<key-file.pem>]"
  exit 1
fi

INSTANCE="$1"
GUEST_USER="$2"
KEY_FILE="${3-}"   # empty if not provided

# If a key file was given, ensure it exists
if [[ -n "$KEY_FILE" && ! -f "$KEY_FILE" ]]; then
  echo "Error: key file '$KEY_FILE' not found."
  exit 1
fi

# Get the Instance ID
INSTANCE_ID=$(openstack server show "$INSTANCE" -f value -c id)
# Exnsure the instance exists
if [[ -z "$INSTANCE_ID" ]]; then
  echo "Instance '$INSTANCE' not found."
  exit 1
fi

# Get the Port ID for that instance - assuming the instance has only one port
PORT_ID=$(openstack port list --server "$INSTANCE_ID" \
  --device-owner compute:nova --format value -c ID | head -1)

# Ensure the port exists
if [[ -z "$PORT_ID" ]]; then
  echo "No port found for instance '$INSTANCE'."
  exit 1
fi

# Extract the fixed IP of that port - assuming it has only one fixed IPv4 address
FIXED_IP_JSON=$(openstack port show "$PORT_ID" -f json -c fixed_ips)
# Parse out "ip_address": "x.x.x.x"
FIXED_IP=$(echo "$FIXED_IP_JSON" \
  | grep -oP '"ip_address"\s*:\s*"\K[0-9\.]+')

# Ensure the fixed IP was found
if [[ -z "$FIXED_IP" ]]; then
  echo "No fixed IP found for port '$PORT_ID'."
  exit 1
fi

# Find the Neutron router interface port on that network
# (device_owner = network:router_interface)
NETWORK_ID=$(openstack port show "$PORT_ID" -f value -c network_id)
ROUTER_PORT_ID=$(openstack port list \
  --network "$NETWORK_ID" \
  --device-owner network:router_interface \
  --format value -c ID)

# Ensure the router port exists
if [[ -z "$ROUTER_PORT_ID" ]]; then
  NETWORK_NAME=$(openstack network show "$NETWORK_ID" -f value -c name)
  echo "No router port found for Instance's network '$NETWORK_NAME'."
  echo "This script requires that a Router is connected to the Network."
  echo "Cannot SSH to $INSTANCE through Network Namespace of a Router."
  exit 1
fi

# 5) Get the Router’s ID (device_id on that port)
ROUTER_ID=$(openstack port show "$ROUTER_PORT_ID" \
  -f value -c device_id)

# 6) Construct the Linux netns name for that router
#    By default Neutron names it qrouter-<router-id>
NETNS="qrouter-${ROUTER_ID}"
# Ensure the netns exists
if ! sudo ip netns list | grep -q "$NETNS"; then
  echo "Network namespace '$NETNS' does not exist."
  exit 1
fi

# 7) Finally: ssh from inside that namespace
echo "SSH’ing to $FIXED_IP as $GUEST_USER via netns $NETNS..."

# Prepare the SSH command with options
# -o StrictHostKeyChecking=no to avoid host key verification prompts
# -i <key-file> if provided, to use the specified private key for authentication

ssh_args=(-o StrictHostKeyChecking=no)
if [[ -n "$KEY_FILE" ]]; then
  ssh_args+=(-i "$KEY_FILE")
fi
ssh_args+=("${GUEST_USER}@${FIXED_IP}")

# Execute the SSH command inside the network namespace
sudo ip netns exec "$NETNS" ssh "${ssh_args[@]}"
