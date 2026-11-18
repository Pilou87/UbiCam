"""Interface web de capture DV : direct, capture en fichier unique, pilotage de la bande."""
import ast
import ctypes
import datetime
import json
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request

from flask import Flask, Response, jsonify, render_template_string, request, send_from_directory

FICHIER_REGLAGES = "/var/lib/dvgrab-web/reglages.json"
JOURNAL = "/tmp/dvgrab.log"
REGLAGES_PAR_DEFAUT = {"dossier": "/srv/captures", "rembobiner": True}


def charger_reglages():
    try:
        with open(FICHIER_REGLAGES) as f:
            return {**REGLAGES_PAR_DEFAUT, **json.load(f)}
    except (OSError, ValueError):
        return dict(REGLAGES_PAR_DEFAUT)


def enregistrer_reglages(reglages):
    # Écriture dans un fichier voisin puis renommage : un arrêt en plein
    # milieu ne laisse jamais un fichier de réglages tronqué.
    os.makedirs(os.path.dirname(FICHIER_REGLAGES), exist_ok=True)
    temporaire = FICHIER_REGLAGES + ".tmp"
    with open(temporaire, "w") as f:
        json.dump(reglages, f)
    os.replace(temporaire, FICHIER_REGLAGES)


def dossier_captures():
    return charger_reglages()["dossier"]


def tester_dossier(chemin):
    """Retourne (ok, message, octets_libres) après un vrai essai d'écriture."""
    if not os.path.isabs(chemin) or not os.path.isdir(chemin):
        return False, "Ce dossier n'existe pas", 0
    # Un partage non monté laisse en place un dossier local du même nom : la
    # capture s'y écrirait sans bruit, dans le conteneur au lieu du NAS.
    point = os.path.realpath(chemin)
    while not os.path.ismount(point):
        point = os.path.dirname(point)
    if point == "/" and chemin.startswith("/mnt/"):
        return False, "Le partage NAS n'est pas monté", 0
    essai = os.path.join(chemin, ".essai-dvgrab")
    try:
        with open(essai, "wb") as f:
            f.write(b"x" * 4096)
        os.remove(essai)
    except OSError as erreur:
        return False, f"Écriture impossible : {erreur.strerror}", 0
    stat = os.statvfs(chemin)
    return True, "Écriture possible", stat.f_bavail * stat.f_frsize


os.makedirs(dossier_captures(), exist_ok=True)

app = Flask(__name__)

# Tailles d'une trame DV : 12 séquences DIF en PAL, 10 en NTSC, 150 blocs de 80 octets.
TAILLE_NTSC = 120000
TAILLE_PAL = 144000


def bcd(octet):
    return (octet >> 4) * 10 + (octet & 0x0F)


def lire_infos(trame):
    """Timecode et date/heure de prise de vue d'une trame DV (None si la bande n'en porte pas)."""
    infos = {"timecode": None, "date": None, "heure": None}

    def examiner(pack):
        code = pack[0]
        if code == 0x13 and infos["timecode"] is None and pack[1] != 0xFF:
            infos["timecode"] = (f"{bcd(pack[4] & 0x3F):02d}:{bcd(pack[3] & 0x7F):02d}:"
                                 f"{bcd(pack[2] & 0x7F):02d}:{bcd(pack[1] & 0x3F):02d}")
        elif code == 0x62 and infos["date"] is None and pack[4] != 0xFF:
            jour, mois, annee = bcd(pack[2] & 0x3F), bcd(pack[3] & 0x1F), bcd(pack[4])
            if 1 <= jour <= 31 and 1 <= mois <= 12:
                infos["date"] = (2000 + annee if annee < 70 else 1900 + annee, mois, jour)
        elif code == 0x63 and infos["heure"] is None and pack[4] != 0xFF:
            infos["heure"] = f"{bcd(pack[4] & 0x3F):02d}:{bcd(pack[3] & 0x7F):02d}:{bcd(pack[2] & 0x7F):02d}"

    for sequence in range(len(trame) // 12000):
        base = sequence * 12000
        # Subcode (blocs 1 et 2) : six paquets de 8 octets, le pack en occupe les 5 derniers.
        for bloc in (1, 2):
            for i in range(6):
                debut = base + bloc * 80 + 3 + i * 8 + 3
                examiner(trame[debut:debut + 5])
        # VAUX (blocs 3 à 5) : quinze packs de 5 octets.
        for bloc in (3, 4, 5):
            for i in range(15):
                debut = base + bloc * 80 + 3 + i * 5
                examiner(trame[debut:debut + 5])
        if all(infos.values()):
            break
    return infos


def dernier_timecode(chemin):
    """Timecode de la dernière image portant un timecode, en remontant depuis la fin."""
    taille = os.path.getsize(chemin)
    with open(chemin, "rb") as f:
        for rang in range(1, 50):
            if taille < rang * TAILLE_PAL:
                break
            f.seek(taille - rang * TAILLE_PAL)
            timecode = lire_infos(f.read(TAILLE_PAL))["timecode"]
            if timecode:
                return timecode
    return None


# Le nom saisi est un gabarit : la date de prise de vue prend la place de ce
# repère, en tête du nom pour que les fichiers se classent dans l'ordre du temps.
REPERE_DATE = "{YYYY-MM-DD}"
REPERE_TITRE = "[Titre]"
NOM_PAR_DEFAUT = f"{REPERE_DATE} - {REPERE_TITRE} - Bande_"


def gabarit(nom):
    """Le nom saisi, avec le repère de date en tête s'il n'en porte pas."""
    # [Titre] laissé tel quel reste dans le nom : il signale un fichier à renommer.
    # Une date tapée en tête (« 1996-06-30 - … ») reprend la place du repère.
    nom = re.sub(r"^\d{4}-\d\d-\d\d", REPERE_DATE, nom)
    return nom if REPERE_DATE in nom else f"{REPERE_DATE} - {nom}"


def date_saisie(nom):
    """La date tapée en tête du nom : elle sert aux images que la bande ne date pas."""
    trouve = re.match(r"^(\d{4})-(\d\d)-(\d\d)", nom)
    if trouve:
        annee, mois, jour = map(int, trouve.groups())
        if 1 <= mois <= 12 and 1 <= jour <= 31:
            return (annee, mois, jour)
    return None


def sans_date(nom):
    """Le nom d'un fichier dont la date n'est pas encore lue sur la bande."""
    # Le séparateur qui suit le repère part avec lui : « {YYYY-MM-DD} - » comme « {YYYY-MM-DD}_ ».
    return re.sub(re.escape(REPERE_DATE) + r"[\s_-]*", "", nom).strip(" -")


def fichier_existant(dossier, nom):
    """Le dernier fichier écrit d'une capture déjà commencée sous ce nom, s'il existe."""
    # Le repère de date accepte n'importe quel jour : une reprise continue le
    # dernier fichier de la cassette, quel que soit le jour où il s'arrête.
    avant, _, apres = nom.partition(REPERE_DATE)
    motif = re.compile(re.escape(avant) + r"\d{4}-\d\d-\d\d" + re.escape(apres) + r"\.dv")
    candidats = [os.path.join(dossier, n) for n in os.listdir(dossier)
                 if n == f"{sans_date(nom)}.dv" or motif.fullmatch(n)]
    return max(candidats, key=os.path.getmtime) if candidats else None


def date_fr(date):
    return f"{date[2]:02d}-{date[1]:02d}-{date[0]}"


def date_iso(date):
    return f"{date[0]}-{date[1]:02d}-{date[2]:02d}"


def commande_bande(commande):
    """Envoie une commande AV/C au caméscope ; retourne (ok, sortie)."""
    try:
        resultat = subprocess.run(["dvcont", *commande.split()], capture_output=True,
                                  text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return False, "Le caméscope ne répond pas"
    sortie = (resultat.stdout + resultat.stderr).strip()
    return resultat.returncode == 0 and "Could not find" not in sortie, sortie


# Bobinage arrière AV/C (WIND, REWIND) : commande de contrôle (00) à la sous-unité
# magnétoscope (20), opcode C4, opérande 65. Réponse 09 = acceptée.
WIND_REWIND = 0x0020C465
SOUS_UNITE_MAGNETO = 4 << 19


def bobiner_arriere():
    """Rembobine en BOBINAGE pur (≈ ×80), jamais en lecture arrière avec image (≈ ×30).

    « dvcont rewind » envoyé sur une platine encore en lecture, ou juste après
    l'arrêt, donne une lecture arrière lente (Bande_114, 16/11/2026). On arrête,
    on laisse la mécanique se poser, on envoie l'ordre WIND directement, puis on
    vérifie que la platine dit bien « Winding reverse » ; sinon on recommence.
    """
    for _ in range(3):
        commande_bande("stop")
        time.sleep(3)
        try:
            raw = ctypes.CDLL("libraw1394.so.11")
            avc = ctypes.CDLL("libavc1394.so.0")
            raw.raw1394_new_handle.restype = ctypes.c_void_p
            raw.raw1394_destroy_handle.argtypes = [ctypes.c_void_p]
            raw.raw1394_set_port.argtypes = [ctypes.c_void_p, ctypes.c_int]
            raw.raw1394_get_nodecount.argtypes = [ctypes.c_void_p]
            avc.avc1394_check_subunit_type.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
            avc.avc1394_transaction.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32, ctypes.c_int]
            avc.avc1394_transaction.restype = ctypes.c_uint32
            poignee = raw.raw1394_new_handle()
            if poignee:
                try:
                    raw.raw1394_set_port(poignee, 0)
                    for noeud in range(raw.raw1394_get_nodecount(poignee)):
                        if avc.avc1394_check_subunit_type(poignee, noeud, SOUS_UNITE_MAGNETO):
                            avc.avc1394_transaction(poignee, noeud, WIND_REWIND, 2)
                            break
                finally:
                    raw.raw1394_destroy_handle(poignee)
        except OSError as erreur:
            print(f"Bobinage AV/C impossible : {erreur}", flush=True)
        time.sleep(3)
        ok, statut = commande_bande("status")
        if ok and "Winding reverse" in statut:
            return True
        if ok and "stop" in statut.lower():
            # Déjà au début, ou ordre ignoré : la position tranche.
            position = position_bande()
            if position is not None and position < 1:
                return True
    # Dernier recours : l'ancienne commande, lente mais qui rembobine quand même.
    commande_bande("rewind")
    return False


class Capture:
    """Une acquisition = un fichier PAR JOUR de prise de vue.

    Tant que la date ne change pas, tout va dans le même fichier, coupures de la
    bande comprises. Une date nouvelle ouvre un fichier où elle remplace {YYYY-MM-DD}.
    Une date déjà vue sur la bande retourne dans SON fichier, à la suite : les
    cassettes alternent parfois les jours, et JF veut un seul fichier par jour.

    Les écritures passent par une file et un fil dédié : une lenteur du partage SMB
    ne doit jamais retarder la lecture du flux, sinon dvgrab perd des trames.
    """

    # Une date lue sur une image abîmée peut être fausse : une date nouvelle ne
    # compte qu'après ce nombre d'images consécutives qui la portent.
    CONFIRMATION = 10
    # Une bande sans date au départ (Bande_117, 18/11/2026) : au bout d'une seconde
    # d'images sans date, le fichier prend la date du jour, à titre provisoire.
    ATTENTE_DATE = 25

    def __init__(self, nom, dossier, reprise=None, date_secours=None):
        self.nom = nom
        # Date tapée par l'utilisateur, prise à la place de la date du jour.
        self.date_secours = date_secours
        self.dossier = dossier
        self.date_courante = None
        # Vrai tant que le fichier porte la date du jour faute de date sur la bande :
        # la première vraie date lue plus loin la remplace.
        self.date_du_jour = False
        self.candidate = None
        self.en_attente = []
        self.en_lecture = False
        self.depart = True
        self.tete = None
        self.ecrites = 0
        self.images_par_seconde = 25
        self.debut = time.time()
        self.dernier_timecode = None
        self.timecode_reprise = None
        # Image au timecode décroché, en attente de la suivante pour savoir si on la garde.
        self.suspecte = None
        # Moment de la dernière image retenue : c'est elle qui dit si la lecture avance encore.
        self.derniere_ecriture = time.time()
        if reprise:
            # On continue un fichier existant : sa date est dans son nom, et
            # son dernier timecode dit où la bande doit repartir.
            self.chemin = reprise
            # Un arrêt brutal peut laisser une image entamée en fin de fichier :
            # on la retire, sinon tout ce qui suit serait décalé.
            with open(reprise, "rb") as f:
                taille_trame = TAILLE_PAL if f.read(4)[3] & 0x80 else TAILLE_NTSC
            taille = os.path.getsize(reprise)
            if taille % taille_trame:
                os.truncate(reprise, taille - taille % taille_trame)
            dates = [(int(a), int(m), int(j)) for a, m, j in
                     re.findall(r"(\d{4})-(\d\d)-(\d\d)", os.path.basename(reprise))]
            if dates:
                self.date_courante = max(dates)
            self.timecode_reprise = dernier_timecode(reprise)
            self.dernier_timecode = self.timecode_reprise
            self.ecrites = os.path.getsize(reprise) // taille_trame
            self.par_date = {self.date_courante: reprise} if self.date_courante else {}
        else:
            self.par_date = {}
            self.chemin = self.chemin_libre(self.nom_pour(None))
        self.fichiers = [self.chemin]
        self.file = queue.Queue(maxsize=600)
        threading.Thread(target=self.ecrire, daemon=True).start()

    def nom_pour(self, date):
        return f"{self.nom.replace(REPERE_DATE, date_iso(date))}.dv" if date else f"{sans_date(self.nom)}.dv"

    def libelle(self):
        """Le nom tel qu'UbiVox le prononce, sans le repère de date."""
        return sans_date(self.nom)

    def chemin_libre(self, nom):
        chemin = os.path.join(self.dossier, nom)
        racine, extension = os.path.splitext(chemin)
        rang = 2
        while os.path.exists(chemin) and chemin != getattr(self, "chemin", None):
            chemin = f"{racine} ({rang}){extension}"
            rang += 1
        return chemin

    def annoncer_date(self, date):
        """La première date connue donne son nom au fichier encore anonyme."""
        if date and self.date_courante is None:
            self.date_courante = date
            self.file.put(("renommer", date))

    @property
    def en_lecture(self):
        return self._en_lecture

    @en_lecture.setter
    def en_lecture(self, valeur):
        # Chaque reprise de la lecture est un départ : bande arrêtée, rien n'arrive
        # pendant la pause, et l'image périmée se présente à la reprise.
        if valeur and not getattr(self, "_en_lecture", False):
            self.depart = True
        self._en_lecture = valeur

    def recevoir(self, trame, infos):
        if not self.en_lecture:
            return
        # Au départ de la lecture, dvgrab peut rendre une image restée de l'arrêt
        # précédent, prise ailleurs sur la bande. On garde la première image le temps
        # de voir la suivante : si leurs timecodes ne se suivent pas, elle est périmée.
        if self.depart:
            self.depart = False
            if infos["timecode"]:
                self.tete = (trame, infos)
                return
        if self.tete:
            trame_tete, infos_tete = self.tete
            self.tete = None
            if not infos["timecode"] or abs(secondes(infos["timecode"]) - secondes(infos_tete["timecode"])) <= 1:
                self.filtrer(trame_tete, infos_tete)
        self.filtrer(trame, infos)

    def filtrer(self, trame, infos):
        """Ne laisse passer que les images d'une lecture normale.

        La platine DSR-1500 envoie des images en permanence, même arrêtée ou en
        rembobinage : sans ce tri, la fin de bande puis l'arrêt finissaient dans les
        fichiers (8 heures de vide sur la Bande_103).
        """
        timecode = infos["timecode"]
        bande = etat.get("bande") or ""
        if not timecode:
            if "Play" not in bande and ("Winding" in bande or "stop" in bande.lower()):
                # Bande arrêtée ou en bobinage : ces images ne viennent pas d'une lecture.
                return
            self.suspecte = None
            self.traiter(trame, infos)
            return
        if self.suspecte:
            trame_suspecte, infos_suspecte = self.suspecte
            self.suspecte = None
            ecart = secondes(timecode) - secondes(infos_suspecte["timecode"])
            if 0 < ecart <= 0.2:
                # L'image décrochée est suivie : c'est une vraie coupure de la bande, on garde tout.
                self.traiter(trame_suspecte, infos_suspecte)
                self.traiter(trame, infos)
                return
        if self.dernier_timecode and abs(secondes(timecode) - secondes(self.dernier_timecode)) > 1:
            # Saut de timecode : rembobinage, avance rapide, ou nouvelle prise ? On attend la suivante.
            self.suspecte = (trame, infos)
            return
        self.traiter(trame, infos)

    def traiter(self, trame, infos):
        # En pause, le caméscope répète la même image avec le même timecode :
        # on ne l'enregistre qu'une fois.
        timecode = infos["timecode"]
        if timecode and timecode == self.dernier_timecode:
            return
        # À la reprise, la bande se cale un peu avant : on saute ce qui est déjà dans le fichier.
        if self.timecode_reprise and timecode and timecode <= self.timecode_reprise:
            return
        self.timecode_reprise = None
        self.dernier_timecode = timecode
        self.images_par_seconde = 25 if len(trame) == TAILLE_PAL else 30
        date = infos["date"]
        if not date and self.date_courante is None and self.ecrites >= self.ATTENTE_DATE:
            self.date_du_jour = True
            self.annoncer_date(self.date_secours or datetime.date.today().timetuple()[:3])
        if date and self.date_courante is None:
            self.annoncer_date(date)
        elif date and self.date_du_jour and date != self.date_courante:
            # Première vraie date de la bande : confirmée, elle remplace la date du
            # jour dans le nom du fichier, qui ne contient que des images sans date.
            if date != self.candidate:
                self.candidate = date
                self.confirmations = 0
            self.confirmations += 1
            if self.confirmations >= self.CONFIRMATION:
                self.date_du_jour = False
                self.date_courante = date
                self.candidate = None
                self.file.put(("renommer", date))
            # L'image reste dans le même fichier, qu'il change de nom ou non.
            self.ajouter(trame)
            return
        elif date and date != self.date_courante:
            # Date nouvelle : on retient les images le temps de la confirmer.
            if date != self.candidate:
                self.vider_attente()
                self.candidate = date
            self.en_attente.append(trame)
            if len(self.en_attente) >= self.CONFIRMATION:
                self.date_courante = date
                self.candidate = None
                self.file.put(("nouveau", date))
                self.vider_attente()
            return
        # Image sans date (passage vierge) ou de la date en cours : elle reste
        # dans le fichier en cours, et une date candidate non confirmée y retourne.
        self.vider_attente()
        self.candidate = None
        self.ajouter(trame)

    def vider_attente(self):
        for trame in self.en_attente:
            self.ajouter(trame)
        self.en_attente = []

    def ajouter(self, trame):
        try:
            self.file.put_nowait(trame)
            self.ecrites += 1
            self.derniere_ecriture = time.time()
        except queue.Full:
            etat["message"] = "Le NAS n'écrit pas assez vite : des images sont perdues"

    def ecrire(self):
        fichier = open(self.chemin, "ab")
        while True:
            element = self.file.get()
            if element is None:
                fichier.close()
                return
            if isinstance(element, tuple):
                action, date = element
                # SMB refuse de renommer un fichier ouvert : on le ferme d'abord.
                fichier.close()
                if action == "renommer":
                    nouveau = self.chemin_libre(self.nom_pour(date))
                    os.rename(self.chemin, nouveau)
                    self.fichiers[self.fichiers.index(self.chemin)] = nouveau
                    # La date du jour provisoire ne désigne plus ce fichier.
                    for jour in [j for j, c in self.par_date.items() if c == self.chemin]:
                        del self.par_date[jour]
                elif date in self.par_date and os.path.exists(self.par_date[date]):
                    # Jour déjà vu sur cette bande : on reprend son fichier, à la suite.
                    nouveau = self.par_date[date]
                else:
                    nouveau = self.chemin_libre(self.nom_pour(date))
                    self.fichiers.append(nouveau)
                self.par_date[date] = nouveau
                self.chemin = nouveau
                fichier = open(self.chemin, "ab")
                continue
            fichier.write(element)

    def terminer(self):
        self.en_lecture = False
        if self.tete:
            self.traiter(*self.tete)
            self.tete = None
        self.vider_attente()
        self.file.put(None)
        while not self.file.empty():
            time.sleep(0.2)
        time.sleep(0.5)
        for chemin in self.fichiers:
            if os.path.exists(chemin) and os.path.getsize(chemin) == 0:
                os.remove(chemin)

    def duree(self):
        return self.ecrites // self.images_par_seconde

    def taille(self):
        try:
            return os.path.getsize(self.chemin)
        except OSError:
            return 0


# Sans image du direct depuis ce délai (secondes) alors qu'il est nourri : on relance.
DIRECT_FIGE = 10


class Direct:
    """Le direct vidéo : ffmpeg ne tourne que tant que quelqu'un regarde."""

    def __init__(self):
        self.spectateurs = 0
        self.image = None
        self.condition = threading.Condition()
        self.processus = None
        self.file = queue.Queue(maxsize=4)
        self.derniere_image = 0

    def nourrir(self, trame):
        if self.spectateurs <= 0:
            return
        if (self.processus is not None and self.processus.poll() is None
                and time.time() - self.derniere_image > DIRECT_FIGE):
            # ffmpeg vivant mais muet alors qu'on le nourrit : le direct est figé
            # (vu le 16/11/2026, après deux jours de marche). On le relance ; la
            # capture, elle, ne passe jamais par lui.
            print("Direct figé : relance de ffmpeg", flush=True)
            self.processus.kill()
            self.processus.wait()
        if self.processus is None or self.processus.poll() is not None:
            self.demarrer()
        try:
            self.file.put_nowait(trame)
        except queue.Full:
            pass  # le direct peut sauter des images, la capture jamais

    def demarrer(self):
        # Délai de grâce : ffmpeg met un instant à sortir sa première image.
        self.derniere_image = time.time()
        self.file = queue.Queue(maxsize=4)
        self.processus = subprocess.Popen(
            ["ffmpeg", "-loglevel", "quiet", "-f", "dv", "-i", "pipe:0", "-an",
             "-vf", "yadif,scale=640:-2", "-r", "12.5", "-q:v", "5",
             "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        processus, file = self.processus, self.file
        threading.Thread(target=self.alimenter, args=(processus, file), daemon=True).start()
        threading.Thread(target=self.decouper, args=(processus,), daemon=True).start()

    def alimenter(self, processus, file):
        while processus.poll() is None:
            try:
                trame = file.get(timeout=5)
            except queue.Empty:
                if self.spectateurs <= 0:
                    break
                continue
            try:
                processus.stdin.write(trame)
            except OSError:
                break
        processus.kill()

    def decouper(self, processus):
        # ffmpeg sort des JPEG bout à bout, délimités par FFD8 … FFD9.
        tampon = b""
        while True:
            morceau = processus.stdout.read1(65536)
            if not morceau:
                return
            tampon += morceau
            while True:
                debut = tampon.find(b"\xff\xd8")
                fin = tampon.find(b"\xff\xd9", debut + 2) if debut >= 0 else -1
                if debut < 0 or fin < 0:
                    break
                with self.condition:
                    self.derniere_image = time.time()
                    self.image = tampon[debut:fin + 2]
                    self.condition.notify_all()
                tampon = tampon[fin + 2:]


etat = {"capture": None, "timecode": None, "date": None, "heure": None, "reception": 0,
        "bande": "inconnu", "message": "", "derniere_trame": None, "position": None}
verrou = threading.Lock()
direct = Direct()


class SonDirect:
    """Le son du direct, en MP3 : seul format que tous les navigateurs lisent en flux continu.

    Il arrive une à deux secondes après l'image (tampon de l'encodeur et du navigateur) :
    c'est un aperçu, pas une surveillance de synchro.
    """

    def __init__(self):
        self.auditeurs = []
        self.processus = None
        self.file = queue.Queue(maxsize=8)

    def nourrir(self, trame):
        if not self.auditeurs:
            return
        if self.processus is None or self.processus.poll() is not None:
            self.demarrer()
        try:
            self.file.put_nowait(trame)
        except queue.Full:
            pass

    def demarrer(self):
        self.file = queue.Queue(maxsize=8)
        # Première paire stéréo seulement : en 32 kHz, la seconde est muette.
        self.processus = subprocess.Popen(
            ["ffmpeg", "-loglevel", "quiet", "-f", "dv", "-i", "pipe:0", "-vn", "-map", "0:a:0",
             "-c:a", "libmp3lame", "-b:a", "128k", "-ar", "48000", "-flush_packets", "1",
             "-f", "mp3", "pipe:1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        processus, file = self.processus, self.file
        threading.Thread(target=self.alimenter, args=(processus, file), daemon=True).start()
        threading.Thread(target=self.diffuser, args=(processus,), daemon=True).start()

    def alimenter(self, processus, file):
        while processus.poll() is None:
            try:
                trame = file.get(timeout=5)
            except queue.Empty:
                if not self.auditeurs:
                    break
                continue
            try:
                processus.stdin.write(trame)
            except OSError:
                break
        processus.kill()

    def diffuser(self, processus):
        while True:
            morceau = processus.stdout.read1(16384)
            if not morceau:
                return
            for auditeur in list(self.auditeurs):
                try:
                    auditeur.put_nowait(morceau)
                except queue.Full:
                    pass  # un auditeur trop lent perd un peu de son, la capture rien


son = SonDirect()


# dvgrab en cours : relancé quand la connexion de la platine est rouverte.
recepteur_courant = {"processus": None}


def canal_platine():
    """Canal de la sortie connectée de la platine (oPCR), ou None si aucune."""
    try:
        resultat = subprocess.run(["python3", os.path.join(os.path.dirname(os.path.abspath(__file__)), "platine.py")],
                                  capture_output=True, text=True, timeout=15)
        for _port, _noeud, en_ligne, _diffusion, connexions, canal, _action in ast.literal_eval(resultat.stdout.strip() or "[]"):
            if en_ligne and connexions:
                return canal
    except Exception as erreur:
        print(f"Canal de la platine illisible : {erreur}", flush=True)
    return None


def recepteur():
    """Lit le caméscope en permanence : le direct en profite, la capture y puise.

    -noavc : dvgrab ne pilote plus la bande, c'est l'interface qui s'en charge.
    """
    while True:
        with open(JOURNAL, "w") as journal:
            # La platine n'émet pas toujours sur le canal 63 attendu par défaut : après
            # un reset du bus, la connexion rouverte peut prendre le canal 0
            # (Bande_116, 17/11/2026). On écoute le canal réellement connecté.
            canal = canal_platine()
            commande = ["dvgrab", "-noavc", "-format", "raw"]
            if canal is not None:
                commande += ["-channel", str(canal)]
            print(f"dvgrab à l'écoute du canal {canal if canal is not None else 63}", flush=True)
            processus = subprocess.Popen(commande + ["-"], stdout=subprocess.PIPE, stderr=journal)
            recepteur_courant["processus"] = processus
            flux = processus.stdout
            while True:
                trame = flux.read(TAILLE_NTSC)
                if len(trame) < TAILLE_NTSC:
                    break
                # Bit DSF de l'en-tête : 1 = PAL, la trame compte deux séquences de plus.
                if trame[3] & 0x80:
                    complement = flux.read(TAILLE_PAL - TAILLE_NTSC)
                    if len(complement) < TAILLE_PAL - TAILLE_NTSC:
                        break
                    trame += complement
                infos = lire_infos(trame)
                etat.update(reception=time.time(), derniere_trame=trame, **infos)
                capture = etat["capture"]
                if capture:
                    capture.recevoir(trame, infos)
                direct.nourrir(trame)
                son.nourrir(trame)
            processus.kill()
            processus.wait()
        time.sleep(3)


# Durée pendant laquelle la bande doit rester arrêtée, sans bouger, avant qu'on
# conclue à la fin de la cassette.
FIN_DE_BANDE = 15
# En lecture, compteur immobile ET plus aucune image reçue depuis ce délai (s) :
# la bande est arrivée au bout sans que la platine se mette d'elle-même à l'arrêt.
BANDE_FIGEE = 60


def surveiller_bande():
    """Relève l'état et la position de la bande ; clôt la capture en fin de cassette.

    Un manque d'images ne clôt JAMAIS une capture : un passage vierge ou une
    coupure peut précéder d'autres images, on va jusqu'au bout de la bande. Le
    caméscope y reste « Playing ». Seule une bande qu'il dit lui-même hors
    lecture, à chaque relevé et sans bouger pendant FIN_DE_BANDE secondes, marque
    la fin de la cassette. Seule exception : une platine restée « Playing » au bout
    de la bande, compteur figé et muette depuis BANDE_FIGEE secondes.
    """
    arretee_depuis = None
    position_arret = None
    # Compteur immobile en pleine lecture : depuis quand.
    position_figee, figee_depuis = None, 0
    # Bande arrêtée, le compteur ne répond plus : on garde le dernier relevé.
    derniere_position = None
    while True:
        time.sleep(2)
        ok, sortie = commande_bande("status")
        etat["bande"] = sortie if ok else "caméscope absent"
        # Le caméscope donne un compteur même sur un passage vierge, sans image :
        # c'est lui qui dit où en est la bande.
        ok_position, sortie_position = commande_bande("timecode")
        trouve = re.findall(r"\d\d:\d\d:\d\d:\d\d", sortie_position) if ok_position else []
        etat["position"] = trouve[-1] if trouve else None
        if trouve:
            derniere_position = trouve[-1]
        capture = etat["capture"]
        if (capture and not capture.en_lecture and ok and "stop" in sortie.lower()
                and "Play" not in sortie and derniere_position and secondes(derniere_position) < 3
                and capture.dernier_timecode and secondes(capture.dernier_timecode) > 60):
            # Capture en pause et bande ramenée au début, loin de la dernière image
            # enregistrée : JF a fini la cassette à la main. On clôt, ce qui lance les .mov.
            clore_capture()
            continue
        if not capture or not capture.en_lecture:
            arretee_depuis = None
            position_figee = None
            continue
        # Une réponse manquée ou vide ne prouve rien : elle ne compte ni pour ni contre.
        if not ok or not sortie or not ok_position:
            continue
        hors_lecture = "Play" not in sortie and "Record" not in sortie
        if not hors_lecture and etat["position"]:
            # La DSR-1500 peut rester « Playing » au bout de la bande, compteur figé
            # et sans plus rien émettre (Bande_114, 16/11/2026). Un passage vierge,
            # lui, fait avancer le compteur : on ne confond pas les deux.
            maintenant = time.time()
            if etat["position"] != position_figee:
                position_figee, figee_depuis = etat["position"], maintenant
            elif (maintenant - figee_depuis > BANDE_FIGEE
                  and maintenant - capture.derniere_ecriture > BANDE_FIGEE
                  and maintenant - etat["reception"] > BANDE_FIGEE):
                print(f"Bande figée à {position_figee} en lecture : fin de cassette", flush=True)
                position_figee = None
                clore_capture(fin_de_bande=True)
                continue
        # On juge sur la dernière image RETENUE : la platine continue d'envoyer
        # des images une fois arrêtée, la réception seule ne prouve rien.
        if not hors_lecture or time.time() - capture.derniere_ecriture < FIN_DE_BANDE:
            arretee_depuis = None
            continue
        if arretee_depuis is None or etat["position"] != position_arret:
            # Une bande qui bouge encore (rembobinage automatique) n'est pas finie.
            arretee_depuis, position_arret = time.time(), etat["position"]
        elif time.time() - arretee_depuis > FIN_DE_BANDE:
            arretee_depuis = None
            if not capture.ecrites:
                # Aucune image depuis le départ : la platine n'a jamais lu la bande
                # (Bande_116, 17/11/2026, bloquée en « Winding » avant le début).
                # Ce n'est pas une fin de cassette : on le dit, sans rembobiner.
                alerte = f"La platine ne lit pas la bande (état « {sortie} ») : capture annulée."
                print(alerte, flush=True)
                clore_capture()
                etat["message"] = alerte + " Vérifie la platine, éjecte et remets la cassette."
                prevenir_ubivox("La platine ne lit pas la bande. Capture annulée, vérifie la platine.")
                continue
            # La bande s'est arrêtée seule en pleine capture : c'est la fin de la
            # cassette. On clôt le fichier et on la ramène au début.
            clore_capture(fin_de_bande=True)


def en_cours():
    return etat["capture"] is not None


def prevenir_ubivox(phrase):
    # Un échec vocal ne doit jamais perturber la capture : on note et on passe.
    url = os.environ.get("UBIVOX_URL", "")
    jeton = os.environ.get("WEBHOOK_TOKEN", "")
    if not url or not jeton:
        return "UbiVox non configuré (URL ou jeton absent)"
    # Sans Content-Length à zéro, nginx répond 411 sur un POST sans corps.
    requete = urllib.request.Request(
        url.rstrip("/") + "/voice/" + urllib.parse.quote(phrase, safe=""),
        data=b"", method="POST",
        headers={"X-Webhook-Token": jeton, "Content-Length": "0"},
    )
    try:
        with urllib.request.urlopen(requete, timeout=30) as reponse:
            return f"UbiVox a répondu {reponse.status}"
    except Exception as erreur:  # noqa: BLE001 — l'annonce est un confort
        return f"UbiVox injoignable : {erreur}"


def lancer_lecture():
    # « play » sur une bande qui lit déjà passe au ralenti : on ne l'envoie qu'à l'arrêt.
    # Après une recherche rapide, le caméscope se dit aussi « Playing » : là, il faut l'envoyer.
    ok, statut = commande_bande("status")
    if ok and "Play" in statut and etat.get("mode_bande") != "recherche":
        etat["mode_bande"] = "lecture"
        return True, "Lecture"
    etat["mode_bande"] = "lecture"
    return commande_bande("play")


def rechercher(sens):
    """Avance ou retour rapide EN IMAGE : la recherche visuelle du caméscope.

    Un bobinage classique (ff/rewind) ne sort aucune image ; la recherche, si. Si
    le caméscope la refuse, on retombe sur le bobinage, sans image mais plus rapide.
    """
    ok, _ = commande_bande(f"trickplay {14 if sens == 'ff' else -14}")
    if ok:
        time.sleep(0.5)
        ok, statut = commande_bande("status")
        if ok and "Play" in statut:
            etat["mode_bande"] = "recherche"
            return True, "recherche"
    etat["mode_bande"] = "bobinage"
    return commande_bande(sens)


def secondes(timecode):
    heures, minutes, secs, images = (int(x) for x in timecode.split(":"))
    return heures * 3600 + minutes * 60 + secs + images / 25


def position_bande():
    ok, sortie = commande_bande("timecode")
    trouve = re.findall(r"\d\d:\d\d:\d\d:\d\d", sortie) if ok else []
    return secondes(trouve[-1]) if trouve else None


def caler_bande(cible, capture):
    """Place la bande quelques secondes avant « cible ».

    Le TRV950E ignore la commande de recherche, mais donne son timecode pendant
    le bobinage (environ 50 fois la vitesse de lecture) : on bobine en le
    surveillant, puis on corrige si on a dépassé. L'arrivée doit tomber AVANT la
    cible ; les images en trop sont sautées à l'écriture.
    """
    but = secondes(cible) - 5
    commande_bande("stop")
    time.sleep(1)
    for _ in range(8):
        position = position_bande()
        if position is None or etat["capture"] is not capture:
            return
        if but - 20 <= position <= but:
            return
        arriere = position > but
        commande_bande("rewind" if arriere else "ff")
        while etat["capture"] is capture:
            time.sleep(0.2)
            position = position_bande()
            if position is None or (arriere and position <= but) or (not arriere and position >= but - 10):
                break
        commande_bande("stop")
        time.sleep(1)
    position = position_bande()
    if position is not None and position > secondes(cible):
        etat["message"] = "Bande calée trop loin : quelques secondes risquent de manquer"


def rembobiner_bande(capture):
    """Ramène la bande au début avant une capture.

    Le TRV950E ignore un « rewind » envoyé juste après « stop » : on laisse la
    mécanique se poser, on vérifie que la bande recule vraiment, et on relance
    l'ordre sinon. Le bobinage n'est fini que lorsque la bande se dit arrêtée ET
    que sa position ne bouge plus : un seul « Winding stopped » ne suffit pas.
    """
    bobiner_arriere()
    position = position_bande()
    if position is not None and position < 1:
        return  # déjà au début
    limite = time.time() + 15 * 60
    precedente = -1
    while time.time() < limite and etat["capture"] is capture:
        time.sleep(2)
        ok, statut = commande_bande("status")
        position = position_bande()
        if ok and "stop" in statut.lower() and position == precedente:
            return
        precedente = position


def demarrer_capture(capture, rembobiner):
    if capture.timecode_reprise:
        etat["message"] = f"Calage de la bande sur {capture.timecode_reprise}…"
        caler_bande(capture.timecode_reprise, capture)
    elif rembobiner:
        etat["message"] = "Rembobinage…"
        rembobiner_bande(capture)
    if etat["capture"] is not capture:
        return
    lancer_lecture()
    capture.en_lecture = True
    etat["message"] = ""


@app.post("/capturer")
def capturer():
    with verrou:
        capture = etat["capture"]
        if capture and capture.en_lecture:
            return jsonify(erreur="Une capture est déjà en cours"), 409
        if capture:
            # Capture suspendue par Stop, Pause ou une recherche : on reprend la lecture
            # là où est la bande, dans le MÊME fichier.
            ok, sortie = lancer_lecture()
            if not ok:
                return jsonify(erreur=sortie or "Le caméscope refuse la lecture"), 502
            capture.en_lecture = True
            return jsonify(fichier=os.path.basename(capture.chemin), reprise=None)
        # Le nom donné est gardé tel quel, accents et espaces compris ; seuls les
        # caractères interdits sur un partage SMB sont remplacés.
        saisi = re.sub(r'[\\/:*?"<>|]', "-", request.form.get("nom", "").strip()) or NOM_PAR_DEFAUT
        nom = gabarit(saisi)
        dossier = dossier_captures()
        ok, message, _ = tester_dossier(dossier)
        if not ok:
            return jsonify(erreur=f"Dossier de capture inutilisable : {message}"), 400
        reprise = fichier_existant(dossier, nom)
        if not reprise and os.path.isdir(dossiers_rangement(dossier)[0]):
            # Une bande déjà rangée revient au dossier de capture pour être continuée.
            rangee = fichier_existant(dossiers_rangement(dossier)[0], nom)
            if rangee:
                reprise = os.path.join(dossier, os.path.basename(rangee))
                os.rename(rangee, reprise)
        capture = Capture(nom, dossier, reprise=reprise, date_secours=date_saisie(saisi))
        etat["capture"] = capture
        # La date de prise de vue est connue dès la première trame reçue, bande à l'arrêt comprise.
        if etat["date"] and time.time() - etat["reception"] < 2:
            capture.annoncer_date(etat["date"])
        threading.Thread(target=demarrer_capture,
                         args=(capture, bool(request.form.get("rembobiner"))), daemon=True).start()
    return jsonify(fichier=os.path.basename(capture.chemin), reprise=capture.timecode_reprise)


# Une bande peut commencer par un passage muet : par défaut, ffmpeg n'en lit que
# le tout début et conclut qu'il n'y a pas de son. On le fait lire plus loin.
SONDE_PROFONDE = ["-probesize", "1G", "-analyzeduration", "600M"]


def frequence_son(chemin):
    sortie = subprocess.run(
        ["ffprobe", "-v", "error", *SONDE_PROFONDE, "-select_streams", "a:0",
         "-show_entries", "stream=sample_rate", "-of", "csv=p=0", chemin],
        capture_output=True, text=True).stdout.strip()
    return int(sortie) if sortie.isdigit() else None


# Position du « type de signal » (stype) dans le source pack VAUX de chaque image.
OCTET_STYPE = 80 * 5 + 48 + 3


class FluxCorrige:
    """Tubes nommés qui servent le .dv à ffmpeg avec un type de signal remis à SD.

    Sur un passage abîmé de la bande, cet octet est souvent faux : à 20, ffmpeg
    croit à du DVCPRO HD, lit QUATRE images d'un coup et le .mov n'en a plus
    qu'un quart (Bande_111, 14/11/2026). La platine ne sort que du DV 25 Mb/s :
    on le ramène à 0 au vol. Le .dv lui-même n'est jamais modifié.
    Chaque tube ne se lit qu'une fois : on en ouvre autant que de lectures.
    """

    def __init__(self, chemin, nombre):
        self.chemin = chemin
        self.dossier = tempfile.mkdtemp(prefix="dvmov-")
        self.tubes = [os.path.join(self.dossier, f"flux{i}.dv") for i in range(nombre)]
        for tube in self.tubes:
            os.mkfifo(tube)
            threading.Thread(target=self.alimenter, args=(tube,), daemon=True).start()

    def alimenter(self, tube):
        # Le tube d'abord : si le .dv ne s'ouvre pas, sa fermeture donne une fin
        # de fichier au lecteur au lieu de le laisser attendre pour toujours.
        try:
            with open(tube, "wb") as sortie, open(self.chemin, "rb") as source:
                taille = TAILLE_PAL if source.read(4)[3] & 0x80 else TAILLE_NTSC
                source.seek(0)
                while True:
                    trame = source.read(taille)
                    if len(trame) < taille:
                        break
                    if trame[OCTET_STYPE] & 0x1f:
                        trame = bytearray(trame)
                        trame[OCTET_STYPE] &= 0xe0
                    sortie.write(trame)
        except (BrokenPipeError, OSError, IndexError):
            # Le lecteur a fini avant la fin du fichier (ffprobe) : normal.
            pass

    def fermer(self):
        # Un tube jamais ouvert par ffmpeg bloquerait son écrivain : on l'ouvre
        # en lecture puis on le referme, l'écrivain reçoit un tube cassé et s'arrête.
        for tube in self.tubes:
            try:
                descripteur = os.open(tube, os.O_RDONLY | os.O_NONBLOCK)
                os.close(descripteur)
            except OSError:
                pass
        time.sleep(0.5)
        for tube in self.tubes:
            try:
                os.remove(tube)
            except OSError:
                pass
        try:
            os.rmdir(self.dossier)
        except OSError:
            pass


def convertir_pour_mac(chemin):
    """Double chaque capture d'un .mov, le format que JF monte dans Final Cut.

    En 32 kHz, le DV enregistre en 12 bits sur QUATRE canaux : la première paire
    porte le son, la seconde (prévue pour le doublage) est muette, et c'est
    celle-là que QuickTime et Final Cut font entendre. Le .mov ne garde que la
    première paire, en 16 bits 48 kHz ; l'image est recopiée telle quelle.
    Le .dv d'origine est conservé à côté.
    """
    if not os.path.exists(chemin):
        return None
    cible = os.path.splitext(chemin)[0] + ".mov"
    partiel = cible + ".part"
    # Le son est lu par une SECONDE lecture du fichier : s'il ne commence qu'après
    # un passage muet, ffmpeg n'a pas à garder en mémoire les images d'ici là. Le
    # début muet est comblé de silence, pour que le son parte de 0 comme l'image.
    # Sans son du tout (rare), on fait quand même le .mov, en image seule.
    # Toutes les lectures passent par le flux corrigé (voir FluxCorrige).
    sonde = FluxCorrige(chemin, 1)
    try:
        avec_son = frequence_son(sonde.tubes[0])
    finally:
        sonde.fermer()
    flux = FluxCorrige(chemin, 2 if avec_son else 1)
    try:
        entree_son, sortie_son = [], []
        if avec_son:
            entree_son = [*SONDE_PROFONDE, "-f", "dv", "-i", flux.tubes[1]]
            sortie_son = ["-map", "1:a:0", "-af", "aresample=async=1:first_pts=0",
                          "-c:a", "pcm_s16le", "-ar", "48000"]
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "dv", "-i", flux.tubes[0],
                        *entree_son, "-map", "0:v:0", *sortie_son, "-c:v", "copy", "-f", "mov",
                        partiel],
                       capture_output=True)
    finally:
        flux.fermer()
    if not os.path.exists(chemin):
        # Le .dv a été supprimé pendant la conversion : le .mov partiel ne sert plus à rien.
        if os.path.exists(partiel):
            os.remove(partiel)
        return None
    # Contrôle : autant d'images dans le .mov que dans le .dv, sinon on n'en veut pas.
    with open(chemin, "rb") as f:
        taille_trame = TAILLE_PAL if f.read(4)[3] & 0x80 else TAILLE_NTSC
    attendues = os.path.getsize(chemin) // taille_trame
    comptees = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-count_packets", "-show_entries",
         "stream=nb_read_packets", "-of", "csv=p=0", partiel], capture_output=True, text=True).stdout.strip()
    if os.path.exists(partiel) and comptees == str(attendues):
        os.replace(partiel, cible)
        return True
    if os.path.exists(partiel):
        os.remove(partiel)
    etat["message"] = f"Conversion en .mov ratée pour {os.path.basename(chemin)}"
    print(etat["message"], flush=True)
    prevenir_ubivox("Attention, la conversion en point mov a échoué.")
    return False


def dossiers_rangement(dossier):
    """Les rayons du rangement : (.dv, .mov).

    Choisis dans la fenêtre des paramètres ; à défaut, les voisins du dossier de
    capture : -MASTER pour les .dv, -MOV pour les .mov.
    """
    reglages = charger_reglages()
    return (reglages.get("dossier_dv") or f"{dossier}-MASTER",
            reglages.get("dossier_mov") or f"{dossier}-MOV")


# Fichiers que le bouton Ranger ne doit pas toucher : une conversion en cours.
en_conversion = set()
# Avancement des .mov, affiché sur la page : sans lui, après le rembobinage de fin
# de bande, rien ne montre que les conversions tournent encore.
avancement = {"faits": 0, "total": 0, "courant": None}


# « 1996-12-27 - Noël chez Mamie - Bande_103 » : date, titre, bande.
MOTIF_NOM = re.compile(r"^(\d{4}-\d\d-\d\d - )(.*?)( - Bande_\S+)$")


def variante(base, rang):
    """Le nom avec « _rang » au bout du titre : « … - Noël chez Mamie_2 - Bande_103 ».

    Un titre qui porte déjà un « _n » repart de son titre nu : « Noël_2 » donne « Noël_3 »."""
    trouve = MOTIF_NOM.match(base)
    debut, titre, fin = trouve.groups() if trouve else ("", base, "")
    titre_nu = re.sub(r"_\d+$", "", titre)
    return f"{debut}{titre_nu}_{rang}{fin}"


def base_libre(base, master, mov):
    """Premier « _2 », « _3 »… dont ni le .dv au MASTER ni le .mov au MOV n'existent."""
    trouve = MOTIF_NOM.match(base)
    titre = trouve.group(2) if trouve else base
    numero = re.search(r"_(\d+)$", titre)
    rang = int(numero.group(1)) + 1 if numero else 2
    while (os.path.exists(os.path.join(master, f"{variante(base, rang)}.dv"))
           or os.path.exists(os.path.join(mov, f"{variante(base, rang)}.mov"))):
        rang += 1
    return variante(base, rang)


def ranger_dossier(dossier):
    """Vide le dossier de capture : les .dv au MASTER, les .mov au MOV.

    Le rangement se fait à la demande : JF renomme d'abord ses fichiers. On ne
    touche ni à la capture en cours, ni à un .mov en train de se faire.

    Règle de JF : un morceau dont le nom existe déjà dans les rayons n'écrase rien,
    c'est peut-être une autre bande du même jour, ou une erreur de nom. Il prend
    « _2 » au bout du titre (« _3 » si « _2 » est pris…), son .dv et son .mov ensemble.
    Seule exception, la reprise : son .dv est revenu du MASTER pour être continué,
    il n'y a donc plus de .dv de ce nom au MASTER et son nouveau .mov remplace l'ancien.
    """
    master, mov = dossiers_rangement(dossier)
    if len({os.path.normpath(dossier), os.path.normpath(master), os.path.normpath(mov)}) < 3:
        raise ValueError("Les rayons de rangement doivent être distincts du dossier de capture")
    for rayon in (master, mov):
        os.makedirs(rayon, exist_ok=True)
    occupes = set(en_conversion)
    capture = etat["capture"]
    if capture:
        occupes.update(os.path.basename(c) for c in capture.fichiers)
    # Un morceau = un nom, avec son .dv et/ou son .mov.
    morceaux = {}
    for nom in os.listdir(dossier):
        base, extension = os.path.splitext(nom)
        if extension in (".dv", ".mov") and not nom.startswith("."):
            morceaux.setdefault(base, []).append(extension)
    deplaces, ignores, renommes = [], [], []
    for base in sorted(morceaux):
        extensions = morceaux[base]
        if any(f"{base}{ext}" in occupes for ext in extensions) or f"{base}.dv" in occupes:
            ignores.extend(f"{base}{ext}" for ext in extensions)
            continue
        if ".dv" in extensions:
            deja_range = os.path.exists(os.path.join(master, f"{base}.dv"))
        else:
            # Un .mov seul : le moindre homonyme rangé, .dv ou .mov, compte.
            deja_range = (os.path.exists(os.path.join(master, f"{base}.dv"))
                          or os.path.exists(os.path.join(mov, f"{base}.mov")))
        nouvelle = base_libre(base, master, mov) if deja_range else base
        if nouvelle != base:
            renommes.append(f"{base} → {nouvelle}")
        for extension in sorted(extensions):
            rayon = master if extension == ".dv" else mov
            os.replace(os.path.join(dossier, f"{base}{extension}"), os.path.join(rayon, f"{nouvelle}{extension}"))
            deplaces.append(f"{nouvelle}{extension}")
    return deplaces, ignores, renommes


def convertir_en_fond(chemins, libelle=None):
    noms = {os.path.basename(c) for c in chemins}
    en_conversion.update(noms)
    avancement["total"] += len(chemins)

    def travailler():
        try:
            for chemin in chemins:
                avancement["courant"] = os.path.basename(chemin)
                convertir_pour_mac(chemin)
                avancement["faits"] += 1
        finally:
            en_conversion.difference_update(noms)
            if not en_conversion:
                avancement.update(faits=0, total=0, courant=None)
                if libelle:
                    prevenir_ubivox(f"Les point mov de {libelle} sont prêts.")
    threading.Thread(target=travailler, daemon=True).start()


def clore_capture(fin_de_bande=False):
    with verrou:
        capture = etat["capture"]
        if not capture:
            return None
        etat["capture"] = None
    commande_bande("stop")
    capture.terminer()
    convertir_en_fond(capture.fichiers, capture.libelle() if capture.ecrites else None)
    if fin_de_bande:
        # Le bobinage attend quelques secondes que la mécanique se pose : à part.
        threading.Thread(target=bobiner_arriere, daemon=True).start()
    if capture.ecrites:
        minutes = capture.duree() // 60
        duree = f"{minutes} minutes" if minutes else f"{capture.duree()} secondes"
        suite = " La cassette est terminée, je la rembobine." if fin_de_bande else ""
        jours = len(capture.fichiers)
        repartition = f", en {jours} fichiers" if jours > 1 else ""
        prevenir_ubivox(f"La capture {capture.libelle()} est terminée : {duree} de vidéo{repartition}.{suite}")
    return capture


@app.post("/arreter")
def arreter():
    capture = clore_capture()
    return jsonify(ok=True, fichier=os.path.basename(capture.chemin) if capture else None)


COMMANDES_BANDE = {"rewind": "Retour rapide", "pause": "Pause", "play": "Lecture",
                   "ff": "Avance rapide", "stop": "Bande arrêtée"}


@app.post("/bande/<commande>")
def bande(commande):
    if commande not in COMMANDES_BANDE:
        return jsonify(erreur="Commande inconnue"), 400
    capture = etat["capture"]
    # Pendant une capture, seule la lecture enregistre : Stop, Pause et les
    # bobinages la suspendent, Lecture la reprend dans le MÊME fichier.
    if capture and commande != "play":
        capture.en_lecture = False
    if commande == "play":
        ok, sortie = lancer_lecture()
    elif commande in ("ff", "rewind"):
        ok, sortie = rechercher(commande)
    else:
        etat["mode_bande"] = commande
        ok, sortie = commande_bande(commande)
    if not ok:
        return jsonify(erreur=sortie or "Commande refusée par le caméscope"), 502
    if capture and commande == "play":
        capture.en_lecture = True
    suite = ""
    if capture:
        suite = " — capture en cours" if capture.en_lecture else " — capture en pause"
    return jsonify(message=COMMANDES_BANDE[commande] + suite)


@app.post("/tester-ubivox")
def tester_ubivox():
    return jsonify(message=prevenir_ubivox("Test de l'interface de capture D V."))


@app.get("/etat")
def etat_courant():
    capture = etat["capture"]
    reponse = {
        "camescope": time.time() - etat["reception"] < 2,
        "bande": etat["bande"], "timecode": etat["timecode"], "heure": etat["heure"],
        "position": etat["position"],
        "date": date_fr(etat["date"]) if etat["date"] else None,
        "message": etat["message"], "actif": capture is not None,
    }
    if avancement["total"] and not etat["message"]:
        reponse["message"] = (f"Fabrication des .mov : {avancement['faits'] + 1}/{avancement['total']}"
                              f" — {avancement['courant'] or ''}")
    if capture:
        reponse.update(date_capture=date_fr(capture.date_courante) if capture.date_courante else None,
                       date_du_jour=capture.date_du_jour, date_imposee=bool(capture.date_secours),
                       fichier=os.path.basename(capture.chemin), en_lecture=capture.en_lecture,
                       duree=capture.duree(), taille=capture.taille())
    return jsonify(reponse)


@app.get("/son")
def flux_son():
    auditeur = queue.Queue(maxsize=200)
    son.auditeurs.append(auditeur)

    def generer():
        try:
            while True:
                try:
                    yield auditeur.get(timeout=5)
                except queue.Empty:
                    continue
        finally:
            son.auditeurs.remove(auditeur)
    return Response(generer(), mimetype="audio/mpeg", headers={"Cache-Control": "no-store"})


@app.get("/direct")
def flux_direct():
    def generer():
        direct.spectateurs += 1
        try:
            derniere = None
            while True:
                with direct.condition:
                    direct.condition.wait(timeout=5)
                    image = direct.image
                if image and image is not derniere:
                    derniere = image
                    yield b"--img\r\nContent-Type: image/jpeg\r\n\r\n" + image + b"\r\n"
        finally:
            direct.spectateurs -= 1
    return Response(generer(), mimetype="multipart/x-mixed-replace; boundary=img",
                    headers={"Cache-Control": "no-store"})


def surveiller_sorties():
    """Les platines (DSR-1500) n'émettent sur le FireWire qu'une fois leur sortie
    connectée, contrairement aux caméscopes. dvgrab tournant en -noavc ne le fait pas :
    on rouvre la connexion dès qu'elle manque (au branchement, après un reset du bus)."""
    while True:
        # Dans un processus à part : libraw1394 laisse des /dev/fw ouverts à chaque
        # passage, et l'appli finissait par ne plus pouvoir ouvrir aucun fichier.
        try:
            resultat = subprocess.run(["python3", os.path.join(os.path.dirname(os.path.abspath(__file__)), "platine.py"), "--connecter"],
                                      capture_output=True, text=True, timeout=15)
            if "connectée" in resultat.stdout or "échec" in resultat.stdout or resultat.returncode:
                print(f"Platine : {resultat.stdout.strip()} {resultat.stderr.strip()[-200:]}", flush=True)
            if "connectée" in resultat.stdout:
                # Nouvelle connexion, canal peut-être différent : dvgrab doit le rejoindre.
                processus = recepteur_courant["processus"]
                if processus and processus.poll() is None:
                    processus.kill()
        except Exception as erreur:
            print(f"Connexion de la platine impossible : {erreur}", flush=True)
        time.sleep(3)


threading.Thread(target=surveiller_sorties, daemon=True).start()
threading.Thread(target=recepteur, daemon=True).start()
threading.Thread(target=surveiller_bande, daemon=True).start()


@app.post("/ranger")
def ranger():
    try:
        deplaces, ignores, renommes = ranger_dossier(dossier_captures())
    except ValueError as erreur:
        return jsonify(erreur=str(erreur)), 409
    return jsonify(deplaces=deplaces, ignores=ignores, renommes=renommes)


@app.get("/fichiers")
def fichiers():
    liste = []
    dossier = dossier_captures()
    for nom in sorted(os.listdir(dossier), reverse=True):
        chemin = os.path.join(dossier, nom)
        # Les fichiers cachés (.DS_Store du Finder, .part en cours) ne sont pas des captures.
        if os.path.isfile(chemin) and not nom.startswith(".") and not nom.endswith(".part"):
            liste.append({"nom": nom, "taille": os.path.getsize(chemin)})
    return jsonify(liste)


@app.get("/telecharger/<nom>")
def telecharger(nom):
    return send_from_directory(dossier_captures(), nom, as_attachment=True)


# Un seul visionnage à la fois : l'encodage à la volée prend du processeur, et une
# capture qui tourne ne doit jamais en manquer.
visionnage = {"processus": None}


@app.get("/duree/<nom>")
def duree_video(nom):
    chemin = os.path.join(dossier_captures(), os.path.basename(nom))
    sortie = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                             "-of", "csv=p=0", chemin], capture_output=True, text=True, timeout=30)
    try:
        return jsonify(duree=int(float(sortie.stdout.strip())))
    except ValueError:
        return jsonify(erreur="Durée illisible"), 400


@app.get("/vignette/<nom>")
def vignette(nom):
    """L'image d'une capture à l'instant t (secondes), pour la réglette qu'on fait glisser.

    Un .dv a des images de taille fixe : on lit directement la bonne, sans que
    ffmpeg ait à parcourir le fichier, et on ne décode qu'elle. Un .mov passe par
    ffmpeg, qui y saute aussi tout droit (chaque image DV est une image clé).
    """
    chemin = os.path.join(dossier_captures(), os.path.basename(nom))
    if not os.path.isfile(chemin):
        return jsonify(erreur="Fichier introuvable"), 404
    instant = max(0.0, request.args.get("t", 0, type=float))
    commun = ["-frames:v", "1", "-vf", "yadif,scale=640:-2", "-q:v", "4", "-f", "mjpeg", "pipe:1"]
    if chemin.endswith(".dv"):
        with open(chemin, "rb") as f:
            entete = f.read(4)
            if len(entete) < 4:
                return jsonify(erreur="Fichier vide"), 400
            pal = entete[3] & 0x80
            taille, cadence = (TAILLE_PAL, 25) if pal else (TAILLE_NTSC, 29.97)
            nombre = os.path.getsize(chemin) // taille
            rang = min(int(instant * cadence), max(0, nombre - 1))
            # Deux images : yadif en a besoin pour en sortir une.
            f.seek(rang * taille)
            trames = bytearray(f.read(taille * 2))
        # Même correction que pour les .mov : un type de signal abîmé égare ffmpeg.
        for debut in range(0, len(trames) - taille + 1, taille):
            trames[debut + OCTET_STYPE] &= 0xe0
        sortie = subprocess.run(["ffmpeg", "-v", "quiet", "-f", "dv", "-i", "pipe:0", *commun],
                                input=bytes(trames), capture_output=True, timeout=15)
    else:
        sortie = subprocess.run(["ffmpeg", "-v", "quiet", "-ss", str(instant), "-i", chemin, *commun],
                                capture_output=True, timeout=15)
    if not sortie.stdout:
        return jsonify(erreur="Image illisible"), 400
    return Response(sortie.stdout, mimetype="image/jpeg", headers={"Cache-Control": "max-age=3600"})


@app.get("/voir/<nom>")
def voir(nom):
    """Le fichier capturé, réencodé à la volée en MP4 lisible par le navigateur.

    Ni le DV ni son .mov (image DV) ne se lisent dans un navigateur. Le MP4 est
    fragmenté pour partir sans attendre la fin ; on ne peut pas s'y déplacer,
    d'où le paramètre « depuis » qui relance la lecture plus loin.
    """
    chemin = os.path.join(dossier_captures(), os.path.basename(nom))
    if not os.path.isfile(chemin):
        return jsonify(erreur="Fichier introuvable"), 404
    depuis = max(0, request.args.get("depuis", 0, type=int))
    ancien = visionnage["processus"]
    if ancien and ancien.poll() is None:
        ancien.kill()
    # La 1re piste son est la bonne : en 32 kHz, la 2e paire du DV est muette.
    processus = subprocess.Popen(
        ["nice", "-n", "10", "ffmpeg", "-loglevel", "quiet", "-ss", str(depuis), "-i", chemin,
         "-map", "0:v:0", "-map", "0:a:0?", "-vf", "yadif,scale=640:-2",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-threads", "2",
         "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
         "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof", "pipe:1"],
        stdout=subprocess.PIPE)
    visionnage["processus"] = processus

    def generer():
        try:
            while True:
                morceau = processus.stdout.read(65536)
                if not morceau:
                    break
                yield morceau
        finally:
            # Fenêtre fermée ou autre position demandée : on arrête l'encodage.
            if processus.poll() is None:
                processus.kill()
            processus.wait()
    return Response(generer(), mimetype="video/mp4")


CARACTERES_INTERDITS = re.compile(r'[\\/:*?"<>|]')


@app.post("/renommer")
def renommer():
    """Renomme une capture avant rangement : le .dv et son .mov changent de nom ensemble."""
    dossier = dossier_captures()
    nom = os.path.basename(request.form.get("nom", ""))
    nouveau = request.form.get("nouveau", "").strip()
    base, extension = os.path.splitext(nom)
    if not os.path.isfile(os.path.join(dossier, nom)):
        return jsonify(erreur="Fichier introuvable"), 404
    if not nouveau or nouveau.startswith(".") or CARACTERES_INTERDITS.search(nouveau):
        return jsonify(erreur='Nom refusé : vide, ou avec un caractère interdit \\ / : * ? " < > |'), 400
    # Le .mov se fabrique à partir du .dv sous son nom : pendant la conversion,
    # renommer l'un ou l'autre les désassortirait.
    capture = etat["capture"]
    occupes = set(en_conversion) | ({os.path.basename(c) for c in capture.fichiers} if capture else set())
    if f"{base}.dv" in occupes or nom in occupes:
        return jsonify(erreur="Capture ou conversion en cours sur ce fichier : réessayez une fois terminée"), 409
    paires = [(f"{base}{ext}", f"{nouveau}{ext}") for ext in (".dv", ".mov")
              if os.path.isfile(os.path.join(dossier, f"{base}{ext}"))]
    for _, cible in paires:
        if os.path.exists(os.path.join(dossier, cible)):
            return jsonify(erreur=f"« {cible} » existe déjà"), 409
    for source, cible in paires:
        os.rename(os.path.join(dossier, source), os.path.join(dossier, cible))
    return jsonify(renommes=[cible for _, cible in paires], extension=extension)


@app.post("/supprimer/<nom>")
def supprimer(nom):
    if en_cours() and nom == os.path.basename(etat["capture"].chemin):
        return jsonify(erreur="Capture en cours"), 409
    chemin = os.path.join(dossier_captures(), os.path.basename(nom))
    if os.path.isfile(chemin):
        os.remove(chemin)
    return jsonify(ok=True)


@app.get("/reglages")
def lire_reglages():
    reglages = charger_reglages()
    ok, message, libre = tester_dossier(reglages["dossier"])
    rayon_dv, rayon_mov = dossiers_rangement(reglages["dossier"])
    return jsonify(**{**reglages, "dossier_dv": rayon_dv, "dossier_mov": rayon_mov},
                   utilisable=ok, message=message, libre=libre)


@app.post("/reglages")
def modifier_reglages():
    reglages = charger_reglages()
    if "dossier" in request.form:
        chemin = os.path.normpath(request.form["dossier"])
        ok, message, libre = tester_dossier(chemin)
        if not ok:
            return jsonify(erreur=message), 400
        if en_cours():
            return jsonify(erreur="Change de dossier une fois la capture terminée"), 409
        reglages["dossier"] = chemin
    for cle, libelle in (("dossier_dv", "rangement des .dv"), ("dossier_mov", "rangement des .mov")):
        if cle not in request.form:
            continue
        valeur = request.form[cle].strip()
        if not valeur:
            # Champ vidé : retour au rayon voisin du dossier de capture.
            reglages.pop(cle, None)
            continue
        chemin = os.path.normpath(valeur)
        ok, message, _ = tester_dossier(chemin)
        if not ok:
            return jsonify(erreur=f"Dossier de {libelle} : {message}"), 400
        reglages[cle] = chemin
    # Un rayon confondu avec le dossier de capture (ou les deux rayons entre eux) :
    # chaque Ranger y retrouverait ses propres fichiers et les renommerait en _2,
    # _3… sans jamais les ranger (Bande_116, 17/11/2026).
    rayon_dv, rayon_mov = (reglages.get("dossier_dv") or f"{reglages['dossier']}-MASTER",
                           reglages.get("dossier_mov") or f"{reglages['dossier']}-MOV")
    if len({reglages["dossier"], rayon_dv, rayon_mov}) < 3:
        return jsonify(erreur="Les trois emplacements doivent être des dossiers différents"), 400
    if "rembobiner" in request.form:
        reglages["rembobiner"] = request.form["rembobiner"] == "1"
    enregistrer_reglages(reglages)
    return lire_reglages()


@app.get("/parcourir")
def parcourir():
    chemin = os.path.normpath(request.args.get("chemin") or "/")
    if not os.path.isdir(chemin):
        return jsonify(erreur="Dossier introuvable"), 404
    try:
        dossiers = sorted(n for n in os.listdir(chemin)
                          if not n.startswith(".") and os.path.isdir(os.path.join(chemin, n)))
    except OSError as erreur:
        return jsonify(erreur=erreur.strerror), 403
    parent = os.path.dirname(chemin) if chemin != "/" else None
    return jsonify(chemin=chemin, parent=parent, dossiers=dossiers)


@app.post("/creer-dossier")
def creer_dossier():
    parent = os.path.normpath(request.form.get("parent", ""))
    nom = os.path.basename(request.form.get("nom", "").strip())
    if not nom or nom.startswith("."):
        return jsonify(erreur="Nom de dossier invalide"), 400
    try:
        os.makedirs(os.path.join(parent, nom), exist_ok=False)
    except OSError as erreur:
        return jsonify(erreur=erreur.strerror), 400
    return jsonify(ok=True)


PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Capture DV</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/core@1.0.0/dist/css/tabler.min.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.19.0/dist/tabler-icons.min.css">
<script src="https://cdn.jsdelivr.net/npm/jquery@3.7.1/dist/jquery.min.js"></script></head>
<body><div class="page-body"><div class="container-xl" style="max-width:900px">
<h2 class="my-3"><i class="ti ti-video"></i> Capture DV</h2>
<div class="card mb-3"><div class="card-body">
 <div class="bg-dark text-center mb-3" style="min-height:270px">
  <img id="apercu" src="/direct" style="max-width:100%"><div id="vide" class="text-secondary pt-6" style="display:none">En attente du caméscope…</div></div>
 <div class="text-center mb-1" title="Compteur du caméscope : il avance même sur un passage vierge"><i class="ti ti-clock"></i> Position de la bande : <b id="position" style="font-family:monospace;font-size:1.2em">--:--:--:--</b></div>
 <div id="infos" class="text-center small text-secondary mb-3"></div>
 <div class="btn-list justify-content-center mb-3">
  <button class="btn bande" data-c="rewind"><i class="ti ti-player-track-prev"></i> Retour rapide</button>
  <button class="btn bande" data-c="pause"><i class="ti ti-player-pause"></i> Pause</button>
  <button class="btn btn-success bande" data-c="play"><i class="ti ti-player-play"></i> Lecture</button>
  <button class="btn bande" data-c="ff"><i class="ti ti-player-track-next"></i> Avance rapide</button>
  <button class="btn bande" data-c="stop"><i class="ti ti-player-stop"></i> Stop</button>
  <button id="ecouter" class="btn" title="Écouter le son du direct sur cet ordinateur"><i class="ti ti-headphones-off"></i> Son coupé</button></div>
 <audio id="son" preload="none"></audio>
 <div class="row g-2 align-items-center">
  <div class="col-md"><input id="nom" class="form-control" value="{YYYY-MM-DD} - [Titre] - Bande_" title="{YYYY-MM-DD} prend la date de prise de vue lue sur la bande ; remplacez [Titre] par le libellé"></div>
  <div class="col-auto"><label class="form-check"><input id="rembobiner" type="checkbox" class="form-check-input">
   <span class="form-check-label">Rembobiner d'abord</span></label></div>
  <div class="col-auto"><button id="demarrer" class="btn btn-danger"><i class="ti ti-player-record"></i> Capturer</button>
   <button id="stop" class="btn btn-secondary" disabled><i class="ti ti-player-stop"></i> Arrêter</button>
   <button id="voix" class="btn" title="Tester l'annonce UbiVox"><i class="ti ti-volume"></i></button></div></div>
 <div id="statut" class="text-secondary mt-3 small"></div></div></div>
<div class="card mb-3"><div class="card-body">
 <div class="row g-2 align-items-center">
  <div class="col-md"><div class="input-group"><span class="input-group-text"><i class="ti ti-folder"></i></span>
   <input id="dossier" class="form-control" readonly></div></div>
  <div class="col-auto"><button id="choisir" class="btn"><i class="ti ti-folder-search"></i> Choisir</button>
   <button id="tester" class="btn"><i class="ti ti-checks"></i> Tester</button></div></div>
 <div id="retour" class="small mt-2"></div></div></div>
<div class="card"><div class="card-header"><h3 class="card-title">Fichiers capturés</h3>
 <div class="card-actions"><button id="ranger" class="btn btn-sm" title="Les .dv vers Captures-DV-MASTER, les .mov vers Captures-DV-MOV"><i class="ti ti-folders"></i> Ranger</button></div></div>
 <div class="table-responsive"><table class="table card-table"><tbody id="liste"></tbody></table></div></div>
</div></div>
<div id="dialogue" class="modal modal-blur" tabindex="-1" style="background:rgba(0,0,0,.5)"><div class="modal-dialog modal-sm modal-dialog-centered"><div class="modal-content">
 <div class="modal-body"><div id="dialogue-texte" style="white-space:pre-line"></div></div>
 <div class="modal-footer"><button id="dialogue-non" class="btn">Annuler</button>
  <button id="dialogue-oui" class="btn btn-primary">Confirmer</button></div></div></div></div>
<div id="lecteur" class="modal modal-blur" tabindex="-1" style="background:rgba(0,0,0,.6)"><div class="modal-dialog modal-lg modal-dialog-centered"><div class="modal-content">
 <div class="modal-header"><h5 id="lecteur-titre" class="modal-title text-truncate"></h5></div>
 <div class="modal-body"><video id="video" controls style="width:100%;background:#000"></video>
  <div class="d-flex align-items-center gap-2 mt-2"><input id="reglette" type="range" class="form-range flex-fill" min="0" max="0" step="1" value="0">
   <span class="text-nowrap" style="font-family:monospace"><span id="lecteur-position">00:00</span><span id="lecteur-duree"></span></span></div>
  <div class="small text-secondary">Faites glisser la réglette pour aller plus loin : la lecture repart de là.</div></div>
 <div class="modal-footer"><button id="lecteur-fermer" class="btn">Fermer</button></div></div></div></div>
<div id="renommage" class="modal modal-blur" tabindex="-1" style="background:rgba(0,0,0,.5)"><div class="modal-dialog modal-dialog-centered"><div class="modal-content">
 <div class="modal-header"><h5 class="modal-title">Renommer la capture</h5></div>
 <div class="modal-body"><div class="input-group"><input id="renommage-nom" class="form-control"><span id="renommage-ext" class="input-group-text"></span></div>
  <div class="small text-secondary mt-2">Le .dv et son .mov sont renommés ensemble.</div></div>
 <div class="modal-footer"><button id="renommage-non" class="btn">Annuler</button>
  <button id="renommage-oui" class="btn btn-primary"><i class="ti ti-check"></i> Renommer</button></div></div></div></div>
<div id="selecteur" class="modal modal-blur" tabindex="-1" style="background:rgba(0,0,0,.5)"><div class="modal-dialog modal-dialog-scrollable"><div class="modal-content">
 <div class="modal-header"><h5 class="modal-title">Choisir le dossier des vidéos</h5></div>
 <div class="modal-body"><div id="chemin" class="text-secondary small mb-2"></div><div id="dossiers" class="list-group"></div>
  <div class="input-group mt-3"><input id="nouveau" class="form-control" placeholder="Nouveau dossier ici">
   <button id="creer" class="btn"><i class="ti ti-folder-plus"></i> Créer</button></div></div>
 <div class="modal-footer"><button id="annuler" class="btn">Annuler</button>
  <button id="valider" class="btn btn-primary"><i class="ti ti-check"></i> Utiliser ce dossier</button></div></div></div></div>
<script>
// Le navigateur intégré bloque les fenêtres du navigateur (la confirmation répond « non » sans rien
// montrer) : la confirmation et les messages passent par une fenêtre de la page.
function ouvrirDialogue(texte,libelle,annulable,siOui){$('#dialogue-texte').text(texte);$('#dialogue-non').toggle(annulable);
 $('#dialogue-oui').text(libelle).off('click').on('click',function(){$('#dialogue').hide();if(siOui)siOui()});$('#dialogue').show().addClass('show')}
function demander(texte,siOui){ouvrirDialogue(texte,'Confirmer',true,siOui)}
function informer(texte){ouvrirDialogue(texte,'OK',false,null)}
$(function(){$('#dialogue-non').click(function(){$('#dialogue').hide()})});
// Les noms portent des apostrophes et des esperluettes (« St Jean-d'acre », « Nath & Anne-Cha »).
function echapper(t){return $('<i>').text(t).html().replace(/"/g,'&quot;')}
function taille(o){return o>1e9?(o/1e9).toFixed(2)+' Go':(o/1e6).toFixed(1)+' Mo'}
function duree(s){return String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0')}
function liste(){$.getJSON('/fichiers',function(l){
 $('#liste').html(l.map(function(f){var n=echapper(f.nom);return '<tr><td>'+n+'</td><td>'+taille(f.taille)+'</td><td class="text-end text-nowrap">'
 +'<button class="btn btn-sm voir" data-n="'+n+'" title="Visualiser"><i class="ti ti-eye"></i></button> '
 +'<button class="btn btn-sm renommer" data-n="'+n+'" title="Renommer (le .dv et son .mov ensemble)"><i class="ti ti-pencil"></i></button> '
 +'<a class="btn btn-sm" href="/telecharger/'+encodeURIComponent(f.nom)+'" title="Télécharger"><i class="ti ti-download"></i></a> '
 +'<button class="btn btn-sm btn-outline-danger sup" data-n="'+f.nom+'"><i class="ti ti-trash"></i></button></td></tr>'}).join('')
 ||'<tr><td class="text-secondary">Aucun fichier</td></tr>')})}
function suivre(){$.getJSON('/etat',function(e){
 $('#demarrer').prop('disabled',e.actif&&e.en_lecture).html(e.actif&&!e.en_lecture
  ?'<i class="ti ti-player-record"></i> Reprendre la capture':'<i class="ti ti-player-record"></i> Capturer');
 $('#stop').prop('disabled',!e.actif);
 $('#apercu').toggle(e.camescope);$('#vide').toggle(!e.camescope);
 $('#position').text(e.position||'--:--:--:--');
 // Un flux MJPEG coupé (redémarrage du service, réseau) ne repart jamais seul :
 // l'image reste « complète » mais vide. On la reconnecte.
 var img=document.getElementById('apercu');
 if(e.camescope&&img.complete&&img.naturalWidth===0)img.src='/direct?'+Date.now();
 $('#infos').text(e.camescope?['Bande : '+e.bande,e.timecode?'Timecode '+e.timecode:'Pas de timecode',
  e.date?'Prise de vue du '+e.date+(e.heure?' à '+e.heure:''):'Pas de date de prise de vue'].join(' — '):e.bande);
 if(e.actif){$('#statut').html('<b>'+(e.en_lecture?'<span class="text-danger">● Capture en cours</span>':'⏸ Capture en pause')+'</b> : '
  +$('<i>').text(e.fichier).html()+' — '+duree(e.duree)+' — '+taille(e.taille)+(e.message?' — '+e.message:''));}
 else $('#statut').text(e.message||'');})}
// Le navigateur n'accepte de jouer du son qu'après un clic : d'où ce bouton.
$('#ecouter').click(function(){var a=document.getElementById('son');
 if(a.paused){a.src='/son?'+Date.now();a.play();$(this).html('<i class="ti ti-headphones"></i> Son actif').addClass('btn-primary');}
 else{a.pause();a.removeAttribute('src');a.load();$(this).html('<i class="ti ti-headphones-off"></i> Son coupé').removeClass('btn-primary');}});
$('#demarrer').click(function(){$.post('/capturer',{nom:$('#nom').val(),rembobiner:$('#rembobiner').is(':checked')?1:''}).fail(function(x){informer(x.responseJSON.erreur)})});
$('#stop').click(function(){$(this).prop('disabled',true);$.post('/arreter',function(r){$('#statut').text('Capture terminée : '+r.fichier);liste()})});
// Visualisation : le serveur réencode à la volée, sans déplacement possible dans le flux ;
// la réglette relance la lecture à la position choisie.
var vu={nom:null,depuis:0};
function lire(depuis){vu.depuis=depuis;var v=document.getElementById('video');
 v.src='/voir/'+encodeURIComponent(vu.nom)+'?depuis='+depuis;v.play().catch(function(){});$('#reglette').val(depuis);montrerPosition()}
function montrerPosition(){var v=document.getElementById('video');$('#lecteur-position').text(duree(Math.floor(vu.depuis+(v.currentTime||0))))}
$(document).on('click','.voir',function(){vu.nom=$(this).data('n');$('#lecteur-titre').text(vu.nom);$('#lecteur-duree').text('');
 $.getJSON('/duree/'+encodeURIComponent(vu.nom),function(r){$('#reglette').attr('max',r.duree);$('#lecteur-duree').text(' / '+duree(r.duree))});
 $('#lecteur').show().addClass('show');lire(0)});
$(function(){
 $('#reglette').on('change',function(){lire(parseInt(this.value,10))});
 $('#video').on('timeupdate',montrerPosition);
 $('#lecteur-fermer').click(function(){var v=document.getElementById('video');v.pause();v.removeAttribute('src');v.load();$('#lecteur').hide()});
 $('#renommage-non').click(function(){$('#renommage').hide()});
 $('#renommage-oui').click(function(){var n=$('#renommage').data('n');
  $.post('/renommer',{nom:n,nouveau:$('#renommage-nom').val()},function(){$('#renommage').hide();liste()})
   .fail(function(x){informer((x.responseJSON&&x.responseJSON.erreur)||'Renommage refusé')})});
 $('#renommage-nom').on('keydown',function(e){if(e.key==='Enter')$('#renommage-oui').click()})});
// Le titre remplace [Titre] : on le sélectionne d'emblée pour le taper par-dessus.
$(document).on('click','.renommer',function(){var n=$(this).data('n'),point=n.lastIndexOf('.'),base=n.slice(0,point);
 $('#renommage').data('n',n).show().addClass('show');$('#renommage-ext').text(n.slice(point));
 var champ=$('#renommage-nom').val(base).focus()[0],i=base.indexOf('[Titre]');
 if(i>=0)champ.setSelectionRange(i,i+7);else champ.select()});
$(document).on('click','.sup',function(){var n=$(this).data('n');demander('Supprimer '+n+' ?',function(){$.post('/supprimer/'+n,liste)})});
var courant='/';
function reglages(){$.getJSON('/reglages',function(r){
 $('#dossier').val(r.dossier);$('#rembobiner').prop('checked',r.rembobiner);
 $('#retour').attr('class','small mt-2 '+(r.utilisable?'text-success':'text-danger'))
  .text(r.utilisable?'Écriture possible — '+taille(r.libre)+' libres':r.message)})}
function ouvrir(c){$.getJSON('/parcourir',{chemin:c},function(d){
 courant=d.chemin;$('#chemin').text(d.chemin);
 var h=d.parent?'<a href="#" class="list-group-item list-group-item-action nav" data-c="'+d.parent+'"><i class="ti ti-arrow-up"></i> ..</a>':'';
 h+=d.dossiers.map(function(n){return '<a href="#" class="list-group-item list-group-item-action nav" data-c="'+(d.chemin==='/'?'':d.chemin)+'/'+n+'"><i class="ti ti-folder"></i> '+n+'</a>'}).join('');
 $('#dossiers').html(h)}).fail(function(x){informer(x.responseJSON.erreur)})}
$(document).on('click','.nav',function(e){e.preventDefault();ouvrir($(this).data('c'))});
$('#choisir').click(function(){$('#selecteur').show().addClass('show');ouvrir($('#dossier').val()||'/')});
$('#annuler').click(function(){$('#selecteur').hide()});
$('#creer').click(function(){$.post('/creer-dossier',{parent:courant,nom:$('#nouveau').val()},function(){$('#nouveau').val('');ouvrir(courant)}).fail(function(x){informer(x.responseJSON.erreur)})});
$('#valider').click(function(){$.post('/reglages',{dossier:courant},function(){$('#selecteur').hide();reglages();liste()}).fail(function(x){informer(x.responseJSON.erreur)})});
$(document).on('click','.bande',function(){var c=$(this).data('c');
 $.post('/bande/'+c,function(r){$('#statut').text(r.message)}).fail(function(x){$('#statut').text(x.responseJSON?x.responseJSON.erreur:'Erreur')})});
$('#tester').click(reglages);
$('#voix').click(function(){$.post('/tester-ubivox',function(r){$('#statut').text(r.message)})});
$('#rembobiner').change(function(){$.post('/reglages',{rembobiner:this.checked?1:0})});
$('#ranger').click(function(){demander('Ranger les .dv dans Captures-DV-MASTER et les .mov dans Captures-DV-MOV ?',function(){
 $.post('/ranger',function(r){var m=r.deplaces.length+' fichier(s) rangé(s)';
  if(r.ignores.length)m+='\\nLaissés en place (capture ou conversion en cours) : '+r.ignores.join(', ');informer(m);liste()})
 .fail(function(x){informer(x.responseJSON?x.responseJSON.erreur:'Rangement impossible')})})});
setInterval(suivre,1500);setInterval(liste,10000);reglages();liste();suivre();
</script></body></html>"""


@app.get("/")
def accueil():
    """La façade de la platine : direct à gauche, captures à droite.
    Lue à chaque appel : on peut la retoucher sans redémarrer le service."""
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "facade.html")


@app.get("/classique")
def accueil_classique():
    """L'ancienne interface : dossier, rangement, suppression."""
    return render_template_string(PAGE)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
