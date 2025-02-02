# AIReminder
Reminder with LLM recognition.

# How to:
## Setup the server
1. Create VM and get ssh via key access `ssh VM_USER@VM_HOST 'echo "PUBLIC_KEY" >> ~/.ssh/authorized_keys'`
2. Generate VM key and add it to the github repo. `ssh VM_USER@VM_HOST 'ssh-keygen && cat ~/.ssh/id_ed25519.pub'`
3. `ssh root@hostname 'bash -s' < bootstrap.sh`
## Deploy the latest service version
1. `./deploy.sh VM_USER VM_HOST`
## Release a new service version
1. Make changes
2. Commit and push them to the master branch
3. `./release.sh`
## Contribute
1. Install python 3.12
2. Install requirements
3. Enjoy