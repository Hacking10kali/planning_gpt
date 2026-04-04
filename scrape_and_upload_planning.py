import asyncio
import json
from datetime import datetime
from pathlib import Path

import aiohttp
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import firebase_admin
from firebase_admin import credentials, firestore


# ── FIREBASE INIT ───────────────────────────────
def init_firebase():
    if not firebase_admin._apps:
        cred = credentials.Certificate("serviceAccountKey.json")
        firebase_admin.initialize_app(cred)
    return firestore.client()


# ── JSON UTILS ────────────────────────────────
def save_json(data, filename: str):
    output_dir = Path("data")
    output_dir.mkdir(exist_ok=True)
    path = output_dir / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"💾 Sauvegardé : {path}")


# ── ID Lookup ─────────────────────────────────
async def get_mal_id(session: aiohttp.ClientSession, titre: str) -> int | None:
    try:
        url = "https://api.jikan.moe/v4/anime"
        params = {"q": titre, "limit": 1}
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            results = data.get("data", [])
            if results:
                mal_id = results[0].get("mal_id")
                return mal_id
    except Exception:
        return None
    return None


async def get_imdb_id(session: aiohttp.ClientSession, titre: str) -> str | None:
    try:
        query = titre.replace(" ", "_")
        url = f"https://v2.sg.media-imdb.com/suggestion/x/{query}.json"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
            results = data.get("d", [])
            for r in results:
                imdb_id = r.get("id", "")
                if imdb_id.startswith("tt"):
                    return imdb_id
    except Exception:
        return None
    return None


async def resolve_ids(session: aiohttp.ClientSession, titre: str) -> dict:
    mal_id = await get_mal_id(session, titre)
    if mal_id:
        return {"mal_id": mal_id, "imdb_id": None}
    imdb_id = await get_imdb_id(session, titre)
    return {"mal_id": None, "imdb_id": imdb_id if imdb_id else None}


# ── SCRAPE PLANNING ───────────────────────────
async def scrape_planning_page(page, session: aiohttp.ClientSession) -> list[dict]:
    print("📅 Extraction du planning...")
    planning_data = []

    jours = await page.query_selector_all("div.fadeJours")
    for jour in jours:
        titre_elem = await jour.query_selector("h2.titreJours")
        titre_jour = (await titre_elem.inner_text()).strip() if titre_elem else "Jour Inconnu"

        jour_data = {"jour": titre_jour, "animes": []}

        cartes = await jour.query_selector_all("div.anime-card-premium")
        for carte in cartes:
            titre_elem = await carte.query_selector(".card-title")
            titre = (await titre_elem.inner_text()).strip() if titre_elem else "Titre Inconnu"

            heure_elem = await carte.query_selector(".info-text.font-bold")
            heure = (await heure_elem.inner_text()).strip() if heure_elem else "Heure Inconnue"

            saison = "Saison Inconnue"
            for info in await carte.query_selector_all(".info-text"):
                cls = await info.get_attribute("class")
                if cls and "font-bold" not in cls:
                    saison = (await info.inner_text()).strip()
                    break

            badge_elem = await carte.query_selector(".badge-text")
            badge = (await badge_elem.inner_text()).strip() if badge_elem else "Inconnu"

            langues = []
            if await carte.query_selector('img[title="VF"]'): langues.append("VF")
            if await carte.query_selector('img[title="VOSTFR"]'): langues.append("VOSTFR")

            ids = await resolve_ids(session, titre)
            await asyncio.sleep(0.5)

            jour_data["animes"].append({
                "titre": titre,
                "heure_sortie": heure,
                "saison": saison,
                "format": badge,
                "langue": " & ".join(langues) if langues else "Inconnue",
                "mal_id": ids["mal_id"],
                "imdb_id": ids["imdb_id"],
            })

        planning_data.append(jour_data)

    total = sum(len(j["animes"]) for j in planning_data)
    print(f"   → {len(planning_data)} jour(s), {total} anime(s) traité(s).")
    return planning_data


# ── MAIN ──────────────────────────────────────
async def main():
    url = "https://anime-sama.to/"

    async with aiohttp.ClientSession() as session:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0.0.0 Safari/537.36",
                locale="fr-FR",
                timezone_id="Europe/Paris"
            )

            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            try:
                await page.wait_for_selector("div.fadeJours", timeout=15000)
            except PlaywrightTimeoutError:
                print("⚠️ Section planning non trouvée.")

            planning = await scrape_planning_page(page, session)
            await browser.close()

    if planning:
        save_json(planning, "planning_anime_sama.json")

        # ── Upload Firestore ─────────────
        db = init_firebase()
        doc_ref = db.collection("planning").document("weekly_planning")
        doc_ref.set({
            "last_update": datetime.utcnow().isoformat(),
            "jours": planning
        })
        print("🔥 Planning mis à jour dans Firestore !")


if __name__ == "__main__":
    asyncio.run(main())
