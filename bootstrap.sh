#!/bin/bash

sudo apt update
sudo apt upgrade -y
sudo apt install -y git python3.12 nohup
sudo mkdir -p /opt/aireminder
sudo chmod a+rwx /opt/aireminder
git clone git@github.com:notanonymousenough/aireminder.git /opt/aireminder
cd /opt/aireminder
python3.12 -m pip install -r requirements.txt