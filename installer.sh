#!/bin/bash
# Installe UbiCam dans un conteneur Debian 12 neuf (à lancer en root DANS le conteneur,
# depuis le dossier UbiCam décompressé).
# Prérequis côté Proxmox : le conteneur voit les /dev/fw* (voir proxmox/creer_conteneur.sh).
set -euo pipefail
cd "$(dirname "$0")"

echo "== Paquets : capture FireWire, pilotage AV/C, conversion, interface web, partage NAS"
apt-get update
apt-get install -y dvgrab libavc1394-tools libiec61883-0 libraw1394-11 ffmpeg python3 python3-flask cifs-utils

echo "== Application"
install -d /opt/dvgrab-web /var/lib/dvgrab-web /etc/dvgrab-web /mnt/nas-dvcam
install -m 644 app/app.py app/facade.html app/platine.py /opt/dvgrab-web/
[ -e /var/lib/dvgrab-web/reglages.json ] || install -m 644 systeme/reglages.json /var/lib/dvgrab-web/

echo "== Secrets (exemples à compléter, jamais écrasés s'ils existent déjà)"
[ -e /etc/dvgrab-web/nas.cred ]    || install -m 600 systeme/nas.cred.exemple /etc/dvgrab-web/nas.cred
[ -e /etc/dvgrab-web/ubivox.env ]  || install -m 600 systeme/ubivox.env.exemple /etc/dvgrab-web/ubivox.env

echo "== Partage DV-CAM du NAS"
grep -q nas-dvcam /etc/fstab || grep -v '^#' systeme/fstab.extrait >> /etc/fstab
systemctl daemon-reload
mount /mnt/nas-dvcam || echo "Montage impossible : compléter /etc/dvgrab-web/nas.cred puis « mount /mnt/nas-dvcam »"

echo "== Service"
install -m 644 systeme/dvweb.service /etc/systemd/system/dvweb.service
systemctl daemon-reload
systemctl enable --now dvweb
systemctl --no-pager status dvweb | head -5
echo "UbiCam : http://$(hostname -I | awk '{print $1}'):8080"
