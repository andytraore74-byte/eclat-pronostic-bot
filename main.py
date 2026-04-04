import os
import logging
import requests
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
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

def get_today_matches():
    today = datetime.utcnow().strftime("%Y-%m-%d")
    data = fdorg_get("/matches", {"dateFrom": today, "dateTo": today, "status": "SCHEDULED"})
    return data.get("matches", []) if data else []

def get_week_matches():
    today = datetime.utcnow()
    end = today + timedelta(days=7)
    data = fdorg_get("/matches", {"dateFrom": today.strftime("%Y-%m-%d"), "dateTo": end.strftime("%Y-%m-%d"), "status": "SCHEDULED"})
    return data.get("matches", []) if data else []

def get_team_last_matches(team_id):
    data = fdorg_get(f"/teams/{team_id}/matches", {"status": "FINISHED", "limit": 8})
    return data.get("matches", []) if data else []

def get_head_to_head(match_id):
    data = fdorg_get(f"/matches/{match_id}/head2head", {"limit": 10})
    return data.get("matches", []) if data else []

# ── API-Football (corners, cards, fouls) ────────────────────────
def apifootball_get(params):
    params["APIkey"] = APIFOOTBALL_KEY
    try:
        r = requests.get(APIFOOTBALL_BASE, params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"APIFootball error: {e}")
        return None

def get_team_stats_apifootball(team_name, season="2024"):
    """Cherche les stats corners/cartons/fautes d'une équipe via son nom."""
    data = apifootball_get({"action": "get_teams", "team_name": team_name, "season_id": season})
    if not data or not isinstance(data, list):
        return None
    return data[0] if data else None

def get_events_stats(home_team_name, away_team_name):
    """Récupère les stats avancées depuis API-Football."""
    home_stats = {"corners": 5, "yellow_cards": 2, "fouls": 12}
    away_stats = {"corners": 4, "yellow_cards": 2, "fouls": 11}

    try:
        # Cherche matchs récents de l'équipe domicile
        data = apifootball_get({
            "action": "get_events",
            "team_name": home_team_name,
            "season_id": "2024"
        })
        if data and isinstance(data, list) and len(data) > 0:
            corners_h = []
            cards_h = []
            fouls_h = []
            for match in data[:8]:
                stats = match.get("statistics", [])
                for s in stats:
                    if s.get("type") == "Corner Kicks":
                        try: corners_h.append(int(s.get("home", 0) or 0))
                        except: pass
                    if s.get("type") == "Yellow Cards":
                        try: cards_h.append(int(s.get("home", 0) or 0))
                        except: pass
                    if s.get("type") == "Fouls":
                        try: fouls_h.append(int(s.get("home", 0) or 0))
                        except: pass
            if corners_h: home_stats["corners"] = sum(corners_h)/len(corners_h)
            if cards_h: home_stats["yellow_cards"] = sum(cards_h)/len(cards_h)
            if fouls_h: home_stats["fouls"] = sum(fouls_h)/len(fouls_h)

        # Cherche matchs récents de l'équipe extérieur
        data2 = apifootball_get({
            "action": "get_events",
            "team_name": away_team_name,
            "season_id": "2024"
        })
        if data2 and isinstance(data2, list) and len(data2) > 0:
            corners_a = []
            cards_a = []
            fouls_a = []
            for match in data2[:8]:
                stats = match.get("statistics", [])
                for s in stats:
                    if s.get("type") == "Corner Kicks":
                        try: corners_a.append(int(s.get("away", 0) or 0))
                        except: pass
                    if s.get("type") == "Yellow Cards":
                        try: cards_a.append(int(s.get("away", 0) or 0))
                        except: pass
                    if s.get("type") == "Fouls":
                        try: fouls_a.append(int(s.get("away", 0) or 0))
                        except: pass
            if corners_a: away_stats["corners"] = sum(corners_a)/len(corners_a)
            if cards_a: away_stats["yellow_cards"] = sum(cards_a)/len(cards_a)
            if fouls_a: away_stats["fouls"] = sum(fouls_a)/len(fouls_a)

    except Exception as e:
        logger.error(f"Stats error: {e}")

    return home_stats, away_stats

# ── Analyse forme ───────────────────────────────────────────────
def analyze_team_form(matches, team_id):
    wins = draws = losses = gf = ga = 0
    for m in matches:
        home_id = m["homeTeam"]["id"]
        score = m.get("score", {}).get("fullTime", {})
        hg = score.get("home") or 0
        ag = score.get("away") or 0
        if team_id == home_id:
            gf += hg; ga += ag
            if hg > ag: wins += 1
            elif hg == ag: draws += 1
            else: losses += 1
        else:
            gf += ag; ga += hg
            if ag > hg: wins += 1
            elif ag == hg: draws += 1
            else: losses += 1
    return wins, draws, losses, gf, ga

def analyze_h2h(h2h_matches, home_id, away_id):
    hw = aw = draws = 0
    for m in h2h_matches:
        score = m.get("score", {}).get("fullTime", {})
        hg = score.get("home") or 0
        ag = score.get("away") or 0
        if m["homeTeam"]["id"] == home_id:
            if hg > ag: hw += 1
            elif hg == ag: draws += 1
            else: aw += 1
        else:
            if ag > hg: hw += 1
            elif ag == hg: draws += 1
            else: aw += 1
    return hw, draws, aw

# ── Prédictions ─────────────────────────────────────────────────
def compute_predictions(home_form, away_form, h2h_home, h2h_draws, h2h_away, home_stats, away_stats):
    hw, hd, hl, hgf, hga = home_form
    aw, ad, al, agf, aga = away_form
    th = hw + hd + hl or 1
    ta = aw + ad + al or 1
    th2h = h2h_home + h2h_draws + h2h_away or 1

    hs = hw/th*0.5 + (hgf-hga)/(th*3+1)*0.3 + h2h_home/th2h*0.2
    as_ = aw/ta*0.5 + (agf-aga)/(ta*3+1)*0.3 + h2h_away/th2h*0.2
    tot = hs + as_ + 0.001
    ph = hs/tot; pa = as_/tot; pd = max(0.05, 1-ph-pa)
    s = ph+pd+pa; ph/=s; pd/=s; pa/=s

    # Buts
    p_btts = min(0.95, (hgf/th)*(agf/ta)*2.5)
    avg_goals = hgf/th + agf/ta
    p_over25 = min(0.95, avg_goals/3.5)
    p_over15 = min(0.97, avg_goals/2.5)
    p_over35 = min(0.90, avg_goals/4.5)

    # Corners
    avg_corners = home_stats["corners"] + away_stats["corners"]
    p_corners_over85 = min(0.95, avg_corners/11.0)
    p_corners_over95 = min(0.95, avg_corners/12.5)

    # Cartons jaunes
    avg_cards = home_stats["yellow_cards"] + away_stats["yellow_cards"]
    p_cards_over25 = min(0.95, avg_cards/4.5)
    p_cards_over35 = min(0.95, avg_cards/5.5)

    # Fautes
    avg_fouls = home_stats["fouls"] + away_stats["fouls"]
    p_fouls_over20 = min(0.95, avg_fouls/25.0)
    p_fouls_over25 = min(0.95, avg_fouls/30.0)

    predictions = {
        "🏠 Victoire domicile": ph,
        "🤝 Match nul": pd,
        "✈️ Victoire extérieur": pa,
        "🔒 Domicile ou Nul (1X)": ph+pd,
        "🔒 Extérieur ou Nul (X2)": pa+pd,
        "⚽ Les deux équipes marquent (BTTS)": p_btts,
        "📈 Plus de 1.5 buts": p_over15,
        "📈 Plus de 2.5 buts": p_over25,
        "📈 Plus de 3.5 buts": p_over35,
        "🚩 Plus de 8.5 corners": p_corners_over85,
        "🚩 Plus de 9.5 corners": p_corners_over95,
        "🟨 Plus de 2.5 cartons jaunes": p_cards_over25,
        "🟨 Plus de 3.5 cartons jaunes": p_cards_over35,
        "⚠️ Plus de 20 fautes": p_fouls_over20,
        "⚠️ Plus de 25 fautes": p_fouls_over25,
    }
    return {k: round(v*100, 1) for k, v in predictions.items()}

# ── Menus ───────────────────────────────────────────────────────
def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Matchs du jour", callback_data="today")],
        [InlineKeyboardButton("📆 Matchs de la semaine", callback_data="week")],
        [InlineKeyboardButton("🔄 Redémarrer", callback_data="wake")],
    ])

# ── Handlers ────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return
    await update.message.reply_text("🤖 *Football Predict Bot*\nChoisissez une option :", parse_mode="Markdown", reply_markup=main_menu_keyboard())

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
        await query.edit_message_text("🤖 *Football Predict Bot*\nChoisissez une option :", parse_mode="Markdown", reply_markup=main_menu_keyboard())
        return

    if data == "today":
        await query.edit_message_text("⏳ Chargement des matchs du jour...")
        matches = get_today_matches()
        if not matches:
            await query.edit_message_text("😴 Aucun match aujourd'hui.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="main_menu")]]))
            return
        context.user_data["matches"] = {str(m["id"]): m for m in matches[:20]}
        buttons = [[InlineKeyboardButton(f"{m['homeTeam']['name'][:14]} vs {m['awayTeam']['name'][:14]}", callback_data=f"predict_{m['id']}")] for m in matches[:20]]
        buttons.append([InlineKeyboardButton("🔙 Retour", callback_data="main_menu")])
        await query.edit_message_text(f"📅 *{len(matches)} matchs aujourd'hui :*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data == "week":
        await query.edit_message_text("⏳ Chargement des matchs de la semaine...")
        matches = get_week_matches()
        if not matches:
            await query.edit_message_text("😴 Aucun match cette semaine.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="main_menu")]]))
            return
        context.user_data["matches"] = {str(m["id"]): m for m in matches[:30]}
        buttons = []
        for m in matches[:30]:
            try:
                dt = datetime.strptime(m["utcDate"], "%Y-%m-%dT%H:%M:%SZ")
                label = f"{dt.strftime('%d/%m')} {m['homeTeam']['name'][:12]} vs {m['awayTeam']['name'][:12]}"
            except:
                label = f"{m['homeTeam']['name'][:14]} vs {m['awayTeam']['name'][:14]}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"predict_{m['id']}")])
        buttons.append([InlineKeyboardButton("🔙 Retour", callback_data="main_menu")])
        await query.edit_message_text(f"📆 *{len(matches)} matchs cette semaine :*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("predict_"):
        match_id = data.split("_")[1]
        match = context.user_data.get("matches", {}).get(match_id)
        if not match:
            await query.edit_message_text("❌ Match introuvable.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="main_menu")]]))
            return

        await query.edit_message_text("⏳ Analyse en cours (corners, cartons, fautes...)...")

        home_id = match["homeTeam"]["id"]
        away_id = match["awayTeam"]["id"]
        home_name = match["homeTeam"]["name"]
        away_name = match["awayTeam"]["name"]

        home_form = analyze_team_form(get_team_last_matches(home_id), home_id)
        away_form = analyze_team_form(get_team_last_matches(away_id), away_id)
        h2h = get_head_to_head(int(match_id))
        h2h_home, h2h_draw, h2h_away = analyze_h2h(h2h, home_id, away_id)

        # Stats avancées
        home_stats, away_stats = get_events_stats(home_name, away_name)

        predictions = compute_predictions(home_form, away_form, h2h_home, h2h_draw, h2h_away, home_stats, away_stats)

        high = {k: v for k, v in predictions.items() if v >= 85}
        lines = [f"📊 *{home_name} vs {away_name}*\n"]

        if high:
            lines.append("✅ *Prédictions > 85% de confiance :*")
            for k, v in sorted(high.items(), key=lambda x: -x[1]):
                lines.append(f"  {k} : *{v}%*")
        else:
            lines.append("⚠️ Aucune prédiction > 85%\n\n📉 *Meilleures options :*")
            for k, v in sorted(predictions.items(), key=lambda x: -x[1])[:5]:
                lines.append(f"  {k} : {v}%")

        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Retour", callback_data="main_menu")]])
        )
        return

# ── Main ────────────────────────────────────────────────────────
def main():
    start_keep_alive()
    threading.Thread(target=heartbeat, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("wake", wake_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    logger.info("🚀 Bot démarré avec corners, cartons et fautes !")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
