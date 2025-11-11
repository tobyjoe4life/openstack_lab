#! /bin/bash

# To run this script in freshly installed Ubuntu 24.04:
# $ wget "https://github.com/tobyjoe4life/openstack_lab/blob/435326f44aef50b4d8febfcbd9cc7d43022a4bdd/2025.1%20(Epoxy)/prep-linux.sh"
# $ bash prep-linux.sh
# Follow on screen instructions

echo "---> Prepare Ubuntu Linux for OpenStack 2025.1 (Epoxy) All-in-One Lab"
echo ""
set -e

echo "---> Configuring openstack user account for passwordless sudo"
echo "openstack ALL=(ALL:ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/openstack
echo "<---"

echo "---> Configuring netplan networking configuration"
cat << EOF | sudo tee /etc/netplan/01-netcfg.yaml
network:
  version: 2
  renderer: networkd
  ethernets:
    ens32:
      dhcp4: true
    ens33:
      dhcp4: false
      addresses: [ 10.0.0.11/24 ]
      routes: []          # no default route via enp0s8
      nameservers:
        addresses: [ 8.8.8.8, 8.8.4.4 ]   # use public DNS for general name resolution
    ens34:
      dhcp4: false        # no IP (Neutron will use this interface)
      optional: true
EOF
sudo netplan apply
sudo rm -f /etc/netplan/50-cloud-init.yaml
echo "<---"

echo "---> Enabling and verifying IP forwarding"
echo "net.ipv4.ip_forward = 1" | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
sudo sysctl net.ipv4.ip_forward
echo "<---"

echo "---> Disabling cloud-init"
sudo touch /etc/cloud/cloud-init.disabled
echo "<---"

echo "---> Installing apt-utils to enable noniteractive apt frontend"
sudo apt install -y apt-utils
export DEBIAN_FRONTEND=noninteractive
echo "<---"

echo "---> Disabling Ubuntu automatic upgrades"
sudo apt remove -y unattended-upgrades
echo "<---"

echo "---> Updating and installing basic packages"
sudo apt update && sudo apt upgrade -y
sudo apt install -y bridge-utils cpu-checker qemu-kvm parted
sudo apt install -y python3-dev python3-venv git libffi-dev gcc libssl-dev libdbus-glib-1-dev vim nano net-tools htop dnsutils yq tree bc
echo "<---"

echo "---> Preparing second disk for Cinder LVM volume groups"
# Initialize the disk as a PV
sudo pvcreate /dev/sdb

# Verify PV creation (optional)
sudo pvs

# Create the first VG using the first partition
sudo vgcreate cinder-volumes /dev/sdb

# Verify VG creation
sudo vgs
echo "<---"

echo "--> Mounting configfs"
echo "configfs /sys/kernel/config configfs defaults 0 0" | sudo tee -a /etc/fstab
sudo mount -a
echo "<---"

echo "---> Making sure DBUS library is installed"
sudo apt install -y python3-dbus
echo "<---"

echo "---> Fetch remaining scripts from GitHub"
curl -L https://github.com/kriscelmer/os_lab/archive/refs/heads/main.tar.gz | \
  tar -xz --wildcards --strip-components=3 --no-anchored "os_lab-main/2025.1 (Epoxy)/scripts/*" -C .
echo "<---"

cat << EOF

Ubuntu Linux is now configured and ready for Kolla Ansible OpenStack deployment.
Shutdown the system with:

sudo shutdown now

(Optionally take the VM snapshot in VMware Workstation Pro.)

Restart the VM and run deploy-openstack.sh script to continue OpenStack Lab deployment.
You can SSH to Ubuntu VM from Windows host with:

ssh openstack@10.0.0.11

You may need to remove old SSH keys with:

ssh-keygen -R 10.0.0.11

EOF
