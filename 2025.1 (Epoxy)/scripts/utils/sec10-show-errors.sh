#!/bin/bash
# Quick health scan for current project

echo "Project: ${OS_PROJECT_NAME:-UNKNOWN}"
date
echo "---------------------------------------"

res=(server volume image network router port)
for r in "${res[@]}"; do
  echo "Checking $r resources for errors:"
  openstack $r list --long -c ID -c name -c status | awk -v rname=$r 'NR>3 && $0 !~ /(ACTIVE|UP|AVAILABLE|active)/ {print rname":",$0}'
done