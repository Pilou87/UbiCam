#!/bin/bash
# Crée le conteneur UbiCam sur l'hôte Proxmox (à lancer en root SUR L'HÔTE), puis l'installe.
# Usage : ./creer_conteneur.sh [numéro] [UbiCam.tar.gz]
# La carte FireWire doit être dans l'hôte : modules firewire_ohci et firewire_core chargés.
set -euo pipefail
ID=${1:-100}
ARCHIVE=${2:-$(dirname "$0")/../UbiCam.tar.gz}
# Adresse du conteneur et passerelle : à renseigner (ex. IP=192.168.1.50/24 PASSERELLE=192.168.1.1 ./creer_conteneur.sh)
IP=${IP:-192.168.xxx.xxx/24}
PASSERELLE=${PASSERELLE:-192.168.xxx.xxx}
case "$IP$PASSERELLE" in *xxx*) echo "Renseigner IP et PASSERELLE avant de lancer le script" >&2; exit 1;; esac
MODELE=local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst

pveam list local | grep -q debian-12-standard || { pveam update; pveam download local debian-12-standard_12.12-1_amd64.tar.zst; }

pct create "$ID" "$MODELE" --hostname dvgrab --cores 2 --memory 1024 --swap 512 \
  --rootfs local-lvm:16 --ostype debian --onboot 1 --features nesting=1,mount=cifs \
  --net0 name=eth0,bridge=vmbr0,gw=$PASSERELLE,ip=$IP,type=veth

# Accès aux périphériques FireWire : leur numéro majeur change au démarrage (234 à 254).
{
  for n in 0 1 2 3; do echo "lxc.mount.entry: /dev/fw$n dev/fw$n none bind,optional,create=file"; done
  for majeur in $(seq 234 254); do echo "lxc.cgroup2.devices.allow: c $majeur:* rwm"; done
} >> "/etc/pve/lxc/$ID.conf"

pct start "$ID"
sleep 5
pct push "$ID" "$ARCHIVE" /root/UbiCam.tar.gz
pct exec "$ID" -- bash -c "cd /root && tar xzf UbiCam.tar.gz && bash UbiCam/installer.sh"
echo "Compléter ensuite /etc/dvgrab-web/nas.cred et ubivox.env dans le conteneur, puis : pct exec $ID -- systemctl restart dvweb"
