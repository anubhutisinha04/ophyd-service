
#!/bin/bash
set -e

# Start LDAP server in docker container
# sudo docker pull osixia/openldap:latest
sudo docker compose -f docker-configs/ldap-docker-compose.yml up -d
sudo docker ps
