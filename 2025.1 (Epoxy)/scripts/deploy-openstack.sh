#! /bin/bash

# To run this script in freshly installed Ubuntu 24.04, first configure the OS by running 'prep-linux.sh' from the same repo
# 'prep-linux./sh' downloads the script into 'scripts' subfolder, otherwise fetch it from the repo:
# $ wget "https://raw.githubusercontent.com/kriscelmer/os_lab/refs/heads/main/2025.1%20(Epoxy)/scripts/deploy-openstack.sh"
# $ bash deploy-openstack.sh
# Script takes 20-45 minutes to complete
# Follow on screen instructions when script finishes

echo "---> Deploy OpenStack 2025.1 (Epoxy) All-in-One Lab using Kolla-Ansible in Docker containers"
echo ""
set -e

echo "---> Creating and activating virtual environment"
cd ~
python3 -m venv --system-site-packages ~/openstack-venv
source ~/openstack-venv/bin/activate
pip install -U --quiet pip
echo "<---"

echo "---> Adding virtual environment activation to .bashrc"
echo "source ~/openstack-venv/bin/activate" >> ~/.bashrc
echo "<---"

echo "---> Installing Kolla-Ansible"
pip install git+https://opendev.org/openstack/kolla-ansible@stable/2025.1
echo "<---"

echo "---> Installing Ansible Galaxy Dependencies"
kolla-ansible install-deps
echo "<---"

echo "---> Copying Configuration and Inventory Templates"
sudo mkdir -p /etc/kolla
sudo chown $USER:$USER /etc/kolla
# Copy example config files
cp -r  ~/openstack-venv/share/kolla-ansible/etc_examples/kolla/* /etc/kolla/
# Copy all-in-one inventory to current directory for editing
cp  ~/openstack-venv/share/kolla-ansible/ansible/inventory/all-in-one .
echo "<---"

echo "---> Generating passwords for OpenStack services and users"
kolla-genpwd
echo "<---"

echo "---> Set password for 'admin' user"
sudo yq -i -y '.keystone_admin_password = "openstack"' /etc/kolla/passwords.yml
echo "<---"

echo "---> Configuring Kolla-Ansible (globals.yml)"
cat << EOF | tee -a /etc/kolla/globals.yml
# ---------------------------------------------------
#
# OpenStack Epoxy All-in-One Lab deployment configuration
#
# Configure Network Interfaces
network_interface: "ens33"
api_interface: "ens33"
neutron_external_interface: "ens34"
dns_interface: "ens33"
kolla_internal_vip_address: "10.0.0.11"
kolla_external_vip_address: "10.0.0.11"

# Configure OpenStack release and base settings
openstack_release: "2025.1"
kolla_base_distro: "ubuntu"
kolla_install_type: "source"

# Disable High Availability
enable_haproxy: "no"
enable_keepalived: "no"
enable_mariadb_proxy: "no"
enable_proxysql: "no"
enable_rabbitmq_cluster: "no"

# Enable Core OpenStack Services
enable_keystone: "yes"
enable_glance: "yes"
enable_nova: "yes"
nova_compute_virt_type: "kvm"
enable_neutron: "yes"
enable_horizon: "yes"
enable_placement: "yes"
enable_cinder: "yes"

# Enable Additional Services
enable_heat: "yes"
enable_horizon_heat: "yes"
enable_skyline: "yes"

# Configure Cinder LVM Backend
enable_cinder_backend_lvm: "yes"

# Configure Designate
enable_designate: "yes"
neutron_dns_domain: "example.test."
neutron_dns_integration: "yes"
designate_backend: "bind9"
designate_ns_record: ["ns1.example.test"]
designate_enable_notifications_sink: "yes"
designate_forwarders_addresses: "8.8.8.8"
enable_horizon_designate: "yes"
EOF
echo "<---"

echo "---> Bootstraping the Server"
kolla-ansible bootstrap-servers -i all-in-one
echo "<---"

echo "---> Running Pre-Deployment Checks"
kolla-ansible prechecks -i all-in-one
echo "<---"

echo "---> Deploying OpenStack Services"
kolla-ansible deploy -i all-in-one
echo "<---"

echo "---> Post-Deployment Tasks"
kolla-ansible post-deploy -i all-in-one
echo "<---"

echo "---> Installing OpenStack CLI Client"
pip install python-openstackclient python-designateclient python-heatclient
echo "<---"

echo "---> Sourcing admin's openrc credentials file"
source /etc/kolla/admin-openrc.sh
echo "<---"

echo "---> Configuring Designate, Neutron and Nova for local DNS support"
openstack zone create --email admin@example.test example.test.
ZONE_ID=$(openstack zone show -f value -c id example.test.)
mkdir -p /etc/kolla/config/designate/
cat << EOF > /etc/kolla/config/designate/designate-sink.conf
[handler:nova_fixed]
zone_id = $ZONE_ID
[handler:neutron_floatingip]
zone_id = $ZONE_ID
EOF
kolla-ansible reconfigure -i all-in-one --tags designate,neutron,nova
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
        addresses: [ 10.0.0.11 ]
        search: [ example.test ]
    ens34:
      dhcp4: false        # no IP (Neutron will use this interface)
      optional: true
EOF
sudo netplan apply
echo "<---"

cat << EOF

OpenStack All-in-One System is now deployed and ready for Lab configuration.

Horizon GUI Console is available from Windows browser at http://10.0.0.11
Skyline modern console is available from Windows browser at http://10.0.0.11:9999

User 'admin' password gets retrieved by running:

grep OS_PASSWORD /etc/kolla/admin-openrc.sh

Admin credentials for OpenStack CLI client are set by running:

source /etc/kolla/admin-openrc.sh

(Optionally shutdown the system now and take the VM snapshot in VMware Workstation Pro, restart the VM.)

Run lab-config.sh script to configure OpenStack Lab.
EOF
