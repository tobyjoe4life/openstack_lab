#! /bin/bash
# Reset all OpenStack resources in the 'troubleshooting' project
echo "---> Resetting all OpenStack resources in the 'troubleshooting' project"
source ~/.demo-troubleshooting-openrc.sh
if [ "$1" != "--force" ]; then
  echo "Following resources will be deleted:"
  openstack project cleanup --dry-run --auth-project
  read -p "Press Enter to continue or Ctrl+C to cancel..."
fi
openstack project cleanup --auto-approve --auth-project
if [ -f "~/sec10-key.pem" ]; then
  openstack keypair delete sec10-key
  rm -f ~/sec10-key.pem
fi
echo "<---"

echo "---> Recreating resources in the 'troubleshooting' project"
openstack network create sec10-net
openstack subnet create sec10-subnet --network sec10-net --subnet-range 10.100.10.0/24 --no-dhcp --dns-nameserver 8.8.8.8 # no DHCP, use public DNS
openstack router create sec10-router
openstack router add subnet sec10-router sec10-subnet # NOTE: no external gateway
openstack security group create sec10-ssh-icmp --description "Security group for Section 10 demo"
openstack security group rule create --ethertype IPv4 --protocol icmp sec10-ssh-icmp
openstack security group rule create --ethertype IPv4 --protocol tcp --dst-port 22 sec10-ssh-icmp
openstack keypair create --private-key ~/sec10-key.pem sec10-key
chmod 600 ~/sec10-key.pem
openstack security group create sec10-icmp-only
openstack security group rule create  --protocol icmp sec10-icmp-only
openstack image create --file images/cirros-0.6.3-x86_64-disk.img --container-format bare --disk-format qcow2 --private --property hypervisor_type=hyperv sec10-cirros-hyperv
openstack server create --flavor m1.tiny --image sec10-cirros-hyperv --key-name sec10-key --security-group sec10-ssh-icmp --network sec10-net sec10-hyperv-vm
openstack server create --flavor m1.tiny --image public-cirros --security-group sec10-icmp-only --network provider-net --password 'Changed!' sec10-no-key
openstack volume create --size 1 sec10-vol
VOL=$(openstack volume show -f value -c id sec10-vol)
source /etc/kolla/admin-openrc.sh
openstack volume set --state error $VOL
echo "<---"