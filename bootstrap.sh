#!/bin/bash

set -e

sudo apt update
sudo apt upgrade -y
sudo timedatectl set-timezone Europe/Moscow
sudo apt install -y git python3.12 python3.12-full python3.12-dev python3.12-venv
sudo rm -frd /opt/aireminder
sudo mkdir -p /opt/aireminder
sudo chown -R $USER:$USER /opt/aireminder
if [ ! -n "$(grep -s "^github.com " ~/.ssh/known_hosts)" ]; then ssh-keyscan github.com >> ~/.ssh/known_hosts 2>/dev/null; fi
git clone git@github.com:notanonymousenough/aireminder.git /opt/aireminder
cd /opt/aireminder
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt