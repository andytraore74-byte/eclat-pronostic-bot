import os
import logging
import requests
import math
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8590894588:AAHBDr4q2rHp0gi_JWxT6ID5iEyh55rISMc")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY", "04c75c996e7842c4bb2d96618adc1db4")
APIFOOTBALL_KEY = os.getenv("APIFOOTBALL_KEY", "d82c475e4a1838beb9a4d6af5f73e6a0935d873051c5ad59ef9d24")
AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID", "7645822721"))

FOOTBALL_API_BASE = "https://api.football-data.org/v4"
APIFOOTBALL_BASE = "https://apiv3.apifootball.com/"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Toutes les compétitions disponibles sur football-data.org gratuit
COMPETITION_IDS = [
    "PL",   # Premier League
    "BL1",  # Bundesliga
    "SA",   # Serie A
    "PD",   # La Liga
    "FL1",  # Ligue 1
    "CL",   # Champions League
    "EL",   # Europa League
    "EC",   # Euro
    "WC",   # Coupe du Monde
    "PPL",  # Primeira Liga (Portugal)
    "DED",  # Eredivisie (Pays-Bas)
    "BSA",  # Brasileirao
]

# ── Keep-alive ──────────────────────────────────────────────────
class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def start_keep_alive():
    def run():
        HTTPServer(("0.0.0.0", 8000), KeepAliveHandler).serve_forever()
    threading.Thread(target=run, daemon=True).start()

def heartbeat():
    while True:
        time.sleep(120)
        try:
            requests.get("http://localhost:8000", timeout=5)
        except:
            pass

def is_authorized(user_id):
    return user_id == AUTHORIZED_USER_ID

# ── Football-data.org API ───────────────────────────────────────
def fdorg_get(endpoint, params=None):
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    try:
        r = requests.get(f"{FOOTBALL_API_BASE}{endpoint}", headers=headers, params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"FD API error: {e}")
        return None

def get_matches_for_date(date_str, status="SCHEDULED"):
    """Récupère tous les matchs d'une date en cherchant dans toutes les compétitions."""
    all_matches = []
    # D'abord essai global
    data = fdorg_get("/matches", {"dateFrom": date_str, "dateTo": date_str})
    if data:
        all_matches = data.get("matches", [])

    # Si pas de résultats, cherche par compétition
    if not all_matches:
        for comp in COMPETITION_IDS:
            data = fdorg_get(f"/competitions/{comp}/matches", {
                "dateFrom": date_str,
                "dateTo": date_str
            })
            if data:
                all_matches.extend(data.get("matches", []))
            time.sleep(0.1)  # évite le rate limiting

    # Filtre par statut
    if status:
        all_matches = [m for m in all_matches if m.get("status") in [status, "TIMED", "IN_PLAY"]]

    # Déduplique par ID
    seen = set()
    unique = []
    for m in all_matches:
        if m["id"] not in seen:
            seen.add(m["id"])
            unique.append(m)

    return unique

def get_today_matches():
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return get_matches_for_date(today)

def get_week_matches():
    matches = []
    today = datetime.utcnow()
    for i in range(7):
        day = (today + timedelta(days=i)).strftime("%Y-%m-%d")
        matches.extend(get_matches_for_date(day))
    return matches

def get_team_last_matches(team_id, limit=10):
    data = fdorg_get(f"/teams/{team_id}/matches", {"status": "FINISHED", "limit": limit})
    return data.get("matches", []) if data else []

def get_head_to_head(match_id):
    data = fdorg_get(f"/matches/{match_id}/head2head", {"limit": 10})
    return data.get("matches", []) if data else []

# ── API-Football ────────────────────────────────────────────────
def apifootball_get(params):
    params["APIkey"] = APIFOOTBALL_KEY
    try:
        r = requests.get(APIFOOTBALL_BASE, params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"APIFootball error: {e}")
        return None

def get_advanced_stats(team_name):
    corners = []
    cards = []
    fouls = []
    data = apifootball_get({"action": "get_events", "team_name": team_name, "season_id": "2024"})
    if data and isinstance(data, list):
        for match in data[:10]:
            for s in match.get("statistics", []):
                t = s.get("type", "")
                try:
                    total = int(str(s.get("home","0") or "0").replace("%","")) + \
                            int(str(s.get("away","0") or "0").replace("%",""))
                    if "Corner" in t: corners.append(total)
                    elif "Yellow Card" in t: cards.append(total)
                    elif "Foul" in t: fouls.append(total)
                except: pass
    return (
        sum(corners)/len(corners) if corners else 5.0,
        sum(cards)/len(cards) if cards else 2.0,
        sum(fouls)/len(fouls) if fouls else 12.0
    )

# ── Modèle de Poisson ───────────────────────────────────────────
def poisson_prob(lam, k):
    try:
        return (math.exp(-lam) * (lam ** k)) / math.factorial(k)
    except:
        return 0

def poisson_over(lam, threshold):
    prob_under = sum(poisson_prob(lam, k) for k in range(int(threshold) + 1))
    return max(0.0, min(1.0, 1 - prob_under))

def compute_weighted_form(matches, team_id):
    goals_scored = []
    goals_conceded = []
    weights = []
    total = len(matches)
    for i, m in enumerate(matches):
        score = m.get("score", {}).get("fullTime", {})
        hg = score.get("home")
        ag = score.get("away")
        if hg is None or ag is None: continue
        hg = int(hg); ag = int(ag)
        is_home = m["homeTeam"]["id"] == team_id
        scored = hg if is_home else ag
        conceded = ag if is_home else hg
        w = (i + 1) / total
        goals_scored.append(scored * w)
        goals_conceded.append(conceded * w)
        weights.append(w)
    if not weights: return 1.2, 1.2
    tw = sum(weights)
    return sum(goals_scored)/tw, sum(goals_conceded)/tw

def compute_form_rating(matches, team_id):
    recent = matches[:5]
    points = 0
    max_pts = len(recent) * 3
    for m in recent:
        score = m.get("score", {}).get("fullTime", {})
        hg = score.get("home")
        ag = score.get("away")
        if hg is None or ag is None: continue
        hg = int(hg); ag = int(ag)
        is_home = m["homeTeam"]["id"] == team_id
        if is_home:
            if hg > ag: points += 3
            elif hg == ag: points += 1
        else:
            if ag > hg: points += 3
            elif ag == hg: points += 1
    return points / max_pts if max_pts > 0 else 0.5

def poisson_predictions(lh, la):
    max_g = 8
    p_hw = p_d = p_aw = p_btts = p_o15 = p_o25 = p_o35 = 0.0
    for i in range(max_g+1):
        for j in range(max_g+1):
            p = poisson_prob(lh, i) * poisson_prob(la, j)
            tg = i + j
            if i > j: p_hw += p
            elif i == j: p_d += p
            else: p_aw += p
            if i > 0 and j > 0: p_btts += p
            if tg > 1.5: p_o15 += p
            if tg > 2.5: p_o25 += p
            if tg > 3.5: p_o35 += p
    return p_hw, p_d, p_aw, p_btts, p_o15, p_o25, p_o35

def compute_all_predictions(home_matches, away_matches, h2h, home_id, away_id,
                             hc, hcard, hf, ac, acard, af):
    home_att, home_def = compute_weighted_form(home_matches, home_id)
    away_att, away_def = compute_weighted_form(away_matches, away_id)
    home_form = compute_form_rating(home_matches, home_id)
    away_form = compute_form_rating(away_matches, away_id)

    hff = 0.8 + (home_form * 0.4)
    aff = 0.8 + (away_form * 0.4)

    lh = max(0.3, home_att * 1.15 * hff * (1 + (1 - away_def)))
    la = max(0.3, away_att * aff * (1 + (1 - home_def)))

    if h2h:
        hg_list, ag_list = [], []
        for m in h2h[:6]:
            score = m.get("score", {}).get("fullTime", {})
            hg = score.get("home"); ag = score.get("away")
            if hg is not None and ag is not None:
                if m["homeTeam"]["id"] == home_id:
                    hg_list.append(int(hg)); ag_list.append(int(ag))
                else:
                    hg_list.append(int(ag)); ag_list.append(int(hg))
        if hg_list:
            lh = lh * 0.7 + (sum(hg_list)/len(hg_list)) * 0.3
            la = la * 0.7 + (sum(ag_list)/len(ag_list)) * 0.3

    p_hw, p_d, p_aw, p_btts, p_o15, p_o25, p_o35 = poisson_predictions(lh, la)

    avg_corners = hc + ac
    avg_cards = hcard + acard
    avg_fouls = hf + af

    predictions = {
        "🏠 Victoire domicile": p_hw,
        "🤝 Match nul": p_d,
        "✈️ Victoire extérieur": p_aw,
        "🔒 Domicile ou Nul (1X)": p_hw + p_d,
        "🔒 Extérieur ou Nul (X2)": p_aw + p_d,
        "⚽ Les deux équipes marquent": p_btts,
        "📈 Plus de 1.5 buts": p_o15,
        "📈 Plus de 2.5 buts": p_o25,
        "📈 Plus de 3.5 buts": p_o35,
        "📉 Moins de 2.5 buts": 1 - p_o25,
        "🚩 Plus de 8.5 corners": poisson_over(avg_corners, 8),
        "🚩 Plus de 9.5 corners": poisson_over(avg_corners, 9),
        "🟨 Plus de 2.5 cartons": poisson_over(avg_cards, 2),
        "🟨 Plus de 3.5 cartons": poisson_over(avg_cards, 3),
        "⚠️ Plus de 20 fautes": poisson_over(avg_fouls, 20),
        "⚠️ Plus de 25 fautes": poisson_over(avg_fouls, 25),
    }

    form_info = {
        "lh": round(lh, 2), "la": round(la, 2),
        "hf": round(home_form*100), "af": round(away_form*100)
    }
    return {k: round(v*100, 1) for k, v in predictions.items()}, form_info

# ── Coupon : 6 meilleurs matchs ─────────────────────────────────
def get_best_prediction_for_match(match):
    """Analyse un match et retourne la meilleure prédiction."""
    try:
        home_id = match["homeTeam"]["id"]
        away_id = match["awayTeam"]["id"]
        home_name = match["homeTeam"]["name"]
        away_name = match["awayTeam"]["name"]

        home_matches = get_team_last_matches(home_id, 8)
        away_matches = get_team_last_matches(away_id, 8)
        h2h = get_head_to_head(match["id"])

        hc, hcard, hf = get_advanced_stats(home_name)
        ac, acard, af = get_advanced_stats(away_name)

        predictions, _ = compute_all_predictions(
            home_matches, away_matches, h2h, home_id, away_id,
            hc, hcard, hf, ac, acard, af
        )
        best_key = max(predictions, key=predictions.get)
        best_val = predictions[best_key]
        return best_key, best_val
    except Exception as e:
        logger.error(f"Coupon error: {e}")
        return None, 0

# ── Menu principal ──────────────────────────────────────────────
def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Matchs du jour", callback_data="today")],
        [InlineKeyboardButton("📆 Matchs de la semaine", callback_data="week")],
        [InlineKeyboardButton("🎯 Coupon du jour (6 matchs)", callback_data="coupon")],
        [InlineKeyboardButton("🔄 Redémarrer", callback_data="wake")],
    ])

# ── Handlers ────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return
    await update.message.reply_text(
        "🤖 *Football Predict Bot*\n📊 Modèle de Poisson + Forme\n\nChoisissez une option :",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )

async def wake_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text("✅ Bot relancé !", reply_markup=main_menu_keyboard())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_authorized(query.from_user.id):
        await query.edit_message_text("⛔ Accès non autorisé.")
        return
    data = query.data

    if data == "wake":
        await query.edit_message_text("✅ Bot relancé !", reply_markup=main_menu_keyboard())
        return

    if data == "main_menu":
        await query.edit_message_text(
            "🤖 *Football Predict Bot*\nChoisissez une option :",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )
        return

    if data == "today":
        await query.edit_message_text("⏳ Recherche de tous les matchs du jour...")
        matches = get_today_matches()
        if not matches:
            await query.edit_message_text(
                "😴 Aucun match trouvé aujourd'hui dans les ligues disponibles.\n\n"
                "Ligues couvertes : Premier League, Bundesliga, Serie A, La Liga, Ligue 1, "
                "Champions League, Europa League, Eredivisie, Primeira Liga, Brasileirao.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="main_menu")]])
            )
            return
        context.user_data["matches"] = {str(m["id"]): m for m in matches[:25]}
        buttons = [[InlineKeyboardButton(
            f"{m['homeTeam']['name'][:13]} vs {m['awayTeam']['name'][:13]}",
            callback_data=f"predict_{m['id']}"
        )] for m in matches[:25]]
        buttons.append([InlineKeyboardButton("🔙 Retour", callback_data="main_menu")])
        await query.edit_message_text(
            f"📅 *{len(matches)} matchs aujourd'hui :*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data == "week":
        await query.edit_message_text("⏳ Recherche des matchs de la semaine...")
        matches = get_week_matches()
        if not matches:
            await query.edit_message_text(
                "😴 Aucun match cette semaine.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="main_menu")]])
            )
            return
        context.user_data["matches"] = {str(m["id"]): m for m in matches[:30]}
        buttons = []
        for m in matches[:30]:
            try:
                dt = datetime.strptime(m["utcDate"], "%Y-%m-%dT%H:%M:%SZ")
                label = f"{dt.strftime('%d/%m')} {m['homeTeam']['name'][:11]} vs {m['awayTeam']['name'][:11]}"
            except:
                label = f"{m['homeTeam']['name'][:13]} vs {m['awayTeam']['name'][:13]}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"predict_{m['id']}")])
        buttons.append([InlineKeyboardButton("🔙 Retour", callback_data="main_menu")])
        await query.edit_message_text(
            f"📆 *{len(matches)} matchs cette semaine :*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data == "coupon":
        await query.edit_message_text(
            "🎯 *Génération du coupon du jour...*\n"
            "⏳ Analyse de tous les matchs en cours (peut prendre 1-2 min)...",
            parse_mode="Markdown"
        )
        matches = get_today_matches()
        if not matches:
            await query.edit_message_text(
                "😴 Aucun match disponible aujourd'hui pour générer un coupon.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="main_menu")]])
            )
            return

        # Analyse chaque match
        scored_matches = []
        for m in matches[:20]:  # max 20 matchs analysés
            best_key, best_val = get_best_prediction_for_match(m)
            if best_key and best_val >= 60:  # seuil minimum 60%
                scored_matches.append({
                    "match": m,
                    "best_key": best_key,
                    "best_val": best_val
                })

        # Trie par meilleur pourcentage
        scored_matches.sort(key=lambda x: -x["best_val"])
        top6 = scored_matches[:6]

        if not top6:
            await query.edit_message_text(
                "⚠️ Impossible de générer un coupon fiable aujourd'hui.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="main_menu")]])
            )
            return

        today_str = datetime.utcnow().strftime("%d/%m/%Y")
        lines = [f"🎯 *COUPON DU JOUR — {today_str}*\n"]
        lines.append("━━━━━━━━━━━━━━━━━━━━")

        for i, item in enumerate(top6, 1):
            m = item["match"]
            home = m["homeTeam"]["name"]
            away = m["awayTeam"]["name"]
            try:
                dt = datetime.strptime(m["utcDate"], "%Y-%m-%dT%H:%M:%SZ")
                time_str = dt.strftime("%H:%M")
            except:
                time_str = "?"
            comp = m.get("competition", {}).get("name", "")
            lines.append(f"\n*{i}. {home} vs {away}*")
            lines.append(f"🏆 {comp} | 🕐 {time_str}")
            lines.append(f"✅ {item['best_key']} : *{item['best_val']}%*")

        lines.append("\n━━━━━━━━━━━━━━━━━━━━")
        lines.append("⚠️ _Pariez de façon responsable_")

        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="main_menu")]])
        )
        return

    if data.startswith("predict_"):
        match_id = data.split("_")[1]
        match = context.user_data.get("matches", {}).get(match_id)
        if not match:
            await query.edit_message_text(
                "❌ Match introuvable.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="main_menu")]])
            )
            return

        home_name = match["homeTeam"]["name"]
        away_name = match["awayTeam"]["name"]
        home_id = match["homeTeam"]["id"]
        away_id = match["awayTeam"]["id"]

        await query.edit_message_text(f"⏳ Analyse de *{home_name}* vs *{away_name}*...", parse_mode="Markdown")

        home_matches = get_team_last_matches(home_id, 10)
        away_matches = get_team_last_matches(away_id, 10)
        h2h = get_head_to_head(int(match_id))
        hc, hcard, hf = get_advanced_stats(home_name)
        ac, acard, af = get_advanced_stats(away_name)

        predictions, form_info = compute_all_predictions(
            home_matches, away_matches, h2h, home_id, away_id,
            hc, hcard, hf, ac, acard, af
        )

        high = {k: v for k, v in predictions.items() if v >= 85}
        lines = [f"📊 *{home_name} vs {away_name}*\n"]
        lines.append(f"📈 Buts attendus: *{form_info['lh']}* - *{form_info['la']}*")
        lines.append(f"💪 Forme: {home_name[:12]} *{form_info['hf']}%* | {away_name[:12]} *{form_info['af']}%*\n")

        if high:
            lines.append("✅ *Prédictions > 85% :*")
            for k, v in sorted(high.items(), key=lambda x: -x[1]):
                lines.append(f"  {k} : *{v}%*")
        else:
            lines.append("⚠️ *Aucune prédiction > 85%*\n\n📉 *Top 5 meilleures options :*")
            for k, v in sorted(predictions.items(), key=lambda x: -x[1])[:5]:
                lines.append(f"  {k} : {v}%")

        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="main_menu")]])
        )
        return

# ── Main ───────────────
