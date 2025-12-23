#!/bin/bash
# Docker Host Setup Script for Gitea Actions

set -e

# Detect package manager
if command -v apt >/dev/null 2>&1; then
    PM=apt
    PM_UPDATE="apt update"
    PM_INSTALL="apt install -y"
elif command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then
    PM=dnf
    PM_UPDATE="dnf check-update"
    PM_INSTALL="dnf install -y"
else
    echo "Unsupported package manager. Need apt or dnf/yum."
    exit 1
fi

echo "Using $PM package manager..."

# Update system
$PM_UPDATE

# Install prerequisites
case $PM in
    apt)
        $PM_INSTALL ca-certificates curl gnupg lsb-release sudoers
        mkdir -p /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
        $PM_UPDATE
        ;;
    dnf)
        $PM_INSTALL dnf-plugins-core
        $PM_INSTALL yum-utils
        $PM config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo
        ;;
esac

# Install Docker and Compose
$PM_INSTALL docker-ce docker-ce-cli containerd.io docker-compose-plugin

# SELinux (Fedora/RHEL)
if command -v dnf >/dev/null 2>&1; then
    $PM_INSTALL container-selinux
fi

# Start and enable Docker
systemctl start docker
systemctl enable docker

# Setup act_runner sudoers
echo "act_runner ALL=(ALL) NOPASSWD: /bin/mkdir, /bin/cp, /usr/bin/chcon, /usr/bin/docker-compose, /usr/bin/docker, /bin/rm, /bin/chmod" | tee /etc/sudoers.d/act_runner
chmod 0440 /etc/sudoers.d/act_runner

# Docker registry login (interactive)
read -p "Enter Docker registry URL (or leave blank): " REGISTRY
read -s -p "Enter Docker registry username: " USERNAME
echo

# Docker registry login
if [[ -n "${DOCKER_REGISTRY_URL}" && -n "${DOCKER_USERNAME}" ]]; then
    mkdir -p /root/.docker
    docker login "${DOCKER_REGISTRY_URL}" -u "${DOCKER_USERNAME}"
    echo "Logged into ${DOCKER_REGISTRY_URL}"
else
    echo "Set DOCKER_REGISTRY_URL and DOCKER_USERNAME env vars first."
fi

echo "Docker host setup complete!"
echo "Restart your Gitea act_runner service for sudoers to take effect."




