#! /bin/bash

# Run this script after succesful OpenStack deployment with 'deploy-openstack.sh' script
# 'prep-linux./sh' downloads the script into 'scripts' subfolder, otherwise fetch it from the repo:
# $ wget "https://raw.githubusercontent.com/kriscelmer/os_lab/refs/heads/main/2025.1%20(Epoxy)/scripts/lab-config.sh"
# $ bash lab-config.sh
# Follow on screen instructions when script finishes

echo "---> Setting user CLI credentials to user admin"
source /etc/kolla/admin-openrc.sh
echo "<---"

echo "---> Activate openstack-venv to enable openstack CLI command"
source ~/openstack-venv/bin/activate
echo "<---"

echo "---> Creating provider network"
openstack network create --share --external --dns-domain example.test. --provider-network-type flat --provider-physical-network physnet1 provider-net
VM_NAT_net_prefix=$(sudo ip -4 -o a show ens32 | awk '{print $4}' | cut -d '.' -f 1,2,3)
openstack subnet create --network provider-net --allocation-pool start=$VM_NAT_net_prefix.100,end=$VM_NAT_net_prefix.127 --gateway $VM_NAT_net_prefix.2 --subnet-range $VM_NAT_net_prefix.0/24 provider-net-subnet
echo "<---"

echo "---> Creating m1.tiny flavor"
openstack flavor create --ram 256 --disk 1 --vcpus 1 m1.tiny
echo "<---"

echo "---> Creating m1.small flavor"
openstack flavor create --ram 512 --disk 5 --vcpus 1 m1.small
echo "<---"

echo "---> Creating m1.standard flavor"
openstack flavor create --ram 1024 --disk 10 --vcpus 1 m1.standard
echo "<---"

echo "---> Creating 'images' subfolder"
mkdir -p ~/images
cd ~/images
echo "<---"

echo "---> Creating a public image public-cirros"
wget https://download.cirros-cloud.net/0.6.3/cirros-0.6.3-x86_64-disk.img
openstack image create --public --file cirros-0.6.3-x86_64-disk.img --disk-format qcow2 --container-format bare public-cirros
cd ..
echo "<---"

echo "---> Creating project demo-project, user demo with role member in demo-project"
openstack project create --enable demo-project
openstack user create --project demo-project --password openstack --ignore-password-expiry demo
openstack role add --user demo --project demo-project member
echo "<---"

echo "---> Creating openrc file for user demo"
cat << EOF > ~/.demo-openrc.sh
# Clear any previous OS_* env vars
for key in \$( set | awk '{FS="="}  /^OS_/ {print \$1}' ); do unset \$key ; done
# Set OS_* env vars for user 'demo' in project 'demo-project'
export OS_PROJECT_DOMAIN_NAME='Default'
export OS_USER_DOMAIN_NAME='Default'
export OS_PROJECT_NAME='demo-project'
export OS_USERNAME='demo'
export OS_AUTH_URL='http://10.0.0.11:5000'
export OS_INTERFACE=public
#export OS_ENDPOINT_TYPE='internalURL'
export OS_IDENTITY_API_VERSION='3'
export OS_REGION_NAME='RegionOne'
export OS_AUTH_PLUGIN='password'
# Read password from the terminal
read -rsp "Please enter OpenStack password for user '\$OS_USERNAME' (in project '\$OS_PROJECT_NAME'): " OS_PASSWORD_INPUT
echo
export OS_PASSWORD=\$OS_PASSWORD_INPUT
unset OS_PASSWORD_INPUT
PS1='[\u@\h \W( demo@demo-project )]\$ '
EOF
echo "<---"

echo "---> Creating example clouds.yaml file"
mkdir -p ~/.config/openstack
cat << EOF > ~/.config/openstack/clouds.yaml
clouds:
  demo:
    auth:
      auth_url: http://10.0.0.11:5000
      project_name: demo-project
      username: demo
      password: openstack
  admin:
    auth:
      auth_url: http://10.0.0.11:5000
      project_name: admin
      username: admin
      password: openstack
EOF
echo "<---"

echo "---> Adding admin user name to shell prompt in admin-openrc.sh"
echo "PS1='[\u@\h \W( admin )]\$ '" >> /etc/kolla/admin-openrc.sh
echo "<---"

echo "---> Sharing example.test. zone with demo-project project"
PROJECT_PROJECT_ID=$(openstack project show -f value -c id demo-project)
openstack zone share create example.test. $PROJECT_PROJECT_ID
echo "<---"

echo "---> Switching CLI credentials to user demo in demo-project project"
source ~/.demo-openrc.sh
echo "<---"

echo "---> Creating demo-net network and demo-router in project demo-project"
openstack network create demo-net
openstack subnet create --network demo-net --subnet-range 10.10.10.0/24 demo-subnet
openstack router create demo-router
openstack router set demo-router --external-gateway provider-net
openstack router add subnet demo-router demo-subnet
echo "<---"

echo "---> Creating private cirros image"
openstack image create --file images/cirros-0.6.3-x86_64-disk.img --disk-format qcow2 --container-format bare demo-cirros
echo "<---"

echo "---> Creating security group allowing ingres of ICMP (ping) and SSH traffic"
openstack security group create --description 'Allows ssh and ping from any host' ssh-icmp
openstack security group rule create --ethertype IPv4 --protocol icmp --remote-ip 0.0.0.0/0 ssh-icmp
openstack security group rule create --ethertype IPv4 --protocol tcp --dst-port 22 --remote-ip 0.0.0.0/0 ssh-icmp
echo "<---"

echo "---> Creating a troubleshoting project and broken resources"
source /etc/kolla/admin-openrc.sh
openstack project create troubleshooting --description "Section 10 demo project"
openstack role add --user demo --project troubleshooting member
cat << EOF > ~/.demo-troubleshooting-openrc.sh
echo "Enabling 'demo@troubleshooting' project credentials"
# Clear any previous OS_* env vars
for key in \$( set | awk '{FS="="}  /^OS_/ {print \$1}' ); do unset \$key ; done
# Set OS_* env vars for user 'demo' in project 'troubleshooting'
export OS_PROJECT_DOMAIN_NAME='Default'
export OS_USER_DOMAIN_NAME='Default'
export OS_PROJECT_NAME='troubleshooting'
export OS_USERNAME='demo'
export OS_AUTH_URL='http://10.0.0.11:5000'
export OS_INTERFACE=public
#export OS_ENDPOINT_TYPE='internalURL'
export OS_IDENTITY_API_VERSION='3'
export OS_REGION_NAME='RegionOne'
export OS_AUTH_PLUGIN='password'
# Read password from the terminal
read -rsp "Please enter OpenStack password for user '\$OS_USERNAME' (in project '\$OS_PROJECT_NAME'): " OS_PASSWORD_INPUT
echo
export OS_PASSWORD=\$OS_PASSWORD_INPUT
unset OS_PASSWORD_INPUT
PS1='[\u@\h \W( demo@troubleshooting )]\$ '
EOF
bash ~/utils/sec10-project-reset.sh --force

cat << EOF

OpenStack All-in-One Lab is configured now.

Horizon GUI Console is available from Windows browser at http://10.0.0.11
Skyline modern console is available from Windows browser at http://10.0.0.11:9999

User 'admin' password gets retrieved by running:

grep OS_PASSWORD /etc/kolla/admin-openrc.sh

Admin credentials for OpenStack CLI client are set by running:

source /etc/kolla/admin-openrc.sh

User demo credentials for OpenStack CLI are set by running:

source ~/.demo-openrc.sh

Enjoy!
EOF
