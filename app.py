from flask import Flask, render_template, request, jsonify, send_file, session, redirect, url_for
import requests
from bs4 import BeautifulSoup
import pandas as pd
import json
from datetime import datetime
from pathlib import Path
import threading
import time
import os
import logging
from dotenv import load_dotenv
from functools import wraps
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'chiave-segreta-da-cambiare-in-produzione')

ACCESS_PASSWORD = os.environ.get('ACCESS_PASSWORD', 'serp2026')

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
EXCEL_FILE = DATA_DIR / "serp_monitoring_results.xlsx"

SEARCH_ENGINES = {
    'google': {'enabled': True, 'domain': 'google.it', 'gl': 'it', 'hl': 'it'},
    'bing':   {'enabled': True, 'market': 'it-IT', 'cc': 'it'}
}

analysis_status = {'running': False, 'progress': 0, 'current_keyword': '', 'results': []}


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


# =============================================================================
# HELPERS
# =============================================================================

def deduplicate(results):
    """Rimuove duplicati per URL (stesso articolo restituito piu volte da SerpAPI)."""
    seen = set()
    out  = []
    for r in results:
        url = r.get('url') or r.get('link', '')
        if url and url not in seen:
            seen.add(url)
            out.append(r)
    return out


# =============================================================================
# GOOGLE NEWS RSS
# Fonte principale per monitoraggio cronologico:
#   - date di pubblicazione REALI (non stimate, non relative)
#   - ordinamento cronologico garantito dal feed
#   - gratuito (nessun credito SerpAPI consumato)
#   - ideale per: "cosa e uscito oggi/questa settimana sulla keyword X"
# =============================================================================

def search_rss(keyword, num_results=50, time_filter=None, sites=None):
    """
    Cerca su Google News RSS.
    Restituisce lista di articoli ordinata per data discendente.
    """
    query = keyword

    # Filtro temporale: Google News RSS supporta il parametro 'when:Xd'
    time_map = {'day': '1d', 'week': '7d', 'month': '30d'}
    if time_filter and time_filter in time_map:
        query = f'{keyword} when:{time_map[time_filter]}'

    if sites:
        site_filter = ' OR '.join([f'site:{s.strip()}' for s in sites])
        query = f'{query} ({site_filter})'

    encoded = quote_plus(query)
    url = f'https://news.google.com/rss/search?q={encoded}&hl=it&gl=IT&ceid=IT:it'

    logging.info(f'📡 Google News RSS: "{query}"')

    results = []
    try:
        resp = requests.get(url, timeout=15, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; SERP-Monitor/1.0)'
        })
        resp.raise_for_status()

        root    = ET.fromstring(resp.content)
        channel = root.find('channel')
        if channel is None:
            logging.warning('RSS: nessun channel trovato nel feed')
            return []

        items = channel.findall('item')
        logging.info(f'  RSS: {len(items)} articoli nel feed')

        for i, item in enumerate(items, 1):
            title   = (item.findtext('title')       or '').strip()
            link    = (item.findtext('link')         or '').strip()
            pub     = (item.findtext('pubDate')      or '').strip()
            desc    = (item.findtext('description')  or '').strip()

            source_el   = item.find('source')
            source_name = (source_el.text.strip()
                           if source_el is not None and source_el.text
                           else 'N/A')

            # Converte pubDate (RFC 2822) in datetime per ordinamento e display
            pub_dt = None
            try:
                pub_dt = parsedate_to_datetime(pub).replace(tzinfo=None)
            except Exception:
                pass

            # Google News RSS include "- NomeFonte" nel titolo: lo rimuoviamo
            clean_title = title
            if source_name != 'N/A' and title.endswith(f' - {source_name}'):
                clean_title = title[:-(len(source_name) + 3)].strip()

            # Rimuove HTML dallo snippet (RSS description puo contenere tag)
            clean_desc = BeautifulSoup(desc, 'html.parser').get_text()[:300] if desc else ''

            results.append({
                'position':    i,
                'title':       clean_title,
                'url':         link,
                'snippet':     clean_desc,
                'date':        pub_dt.strftime('%d/%m/%Y %H:%M') if pub_dt else pub,
                '_date_dt':    pub_dt,   # usato solo per sorting
                'source_name': source_name,
                'source':      'Google News RSS',
            })

        # Ordina per data discendente (piu recente prima)
        results.sort(key=lambda x: x['_date_dt'] or datetime.min, reverse=True)

        # Rimuovi campo interno
        for r in results:
            r.pop('_date_dt', None)

        # Rinumera dopo il sort
        for i, r in enumerate(results, 1):
            r['position'] = i

        logging.info(f'  RSS: restituiti {min(len(results), num_results)} articoli ordinati per data')
        return results[:num_results]

    except ET.ParseError as e:
        logging.error(f'✗ RSS: errore parsing XML: {e}')
        return []
    except Exception as e:
        logging.error(f'✗ Errore RSS: {e}')
        import traceback; logging.error(traceback.format_exc())
        return []


# =============================================================================
# GOOGLE SERP (SerpAPI)
# Fonte per panoramica per rilevanza (non cronologica).
# ATTENZIONE: le date restituite sono relative e approssimative.
# =============================================================================

def search_google(keyword, num_results=30, time_filter=None, sites=None):
    if not SEARCH_ENGINES['google']['enabled']:
        return []

    serpapi_key = os.getenv('SERPAPI_KEY')
    if not serpapi_key:
        logging.error('SERPAPI_KEY non configurata!')
        return []

    query = keyword
    if sites:
        query = f'{keyword} ({" OR ".join([f"site:{s.strip()}" for s in sites])})'

    all_results = []
    pages_needed = (num_results + 9) // 10
    empty_pages  = 0

    try:
        gc = SEARCH_ENGINES['google']
        logging.info(f'🔍 Google SERP: "{keyword}" (target {num_results})')

        for page in range(pages_needed):
            params = {
                'engine': 'google', 'q': query,
                'start': page * 10, 'num': 10,
                'hl': gc['hl'], 'gl': gc['gl'],
                'google_domain': gc['domain'],
                'api_key': serpapi_key,
            }
            if time_filter == 'day':    params['tbs'] = 'qdr:d'
            elif time_filter == 'week': params['tbs'] = 'qdr:w'
            elif time_filter == 'month': params['tbs'] = 'qdr:m'

            resp    = requests.get('https://serpapi.com/search', params=params, timeout=15)
            resp.raise_for_status()
            organic = resp.json().get('organic_results', [])

            if not organic:
                empty_pages += 1
                if empty_pages >= 2: break
                time.sleep(0.5)
                continue

            empty_pages = 0
            for item in organic:
                all_results.append({
                    'position': len(all_results) + 1,
                    'title':    item.get('title',   'N/A') or 'N/A',
                    'url':      item.get('link',    'N/A'),
                    'snippet':  item.get('snippet', '') or '',
                    'date':     item.get('date',    'N/A') or 'N/A',
                    'source':   f"Google.{gc['gl']}",
                })

            if len(all_results) >= num_results: break
            if page < pages_needed - 1: time.sleep(0.5)

        # Deduplica: SerpAPI puo restituire lo stesso URL su pagine diverse
        all_results = deduplicate(all_results)
        for i, r in enumerate(all_results, 1):
            r['position'] = i

        logging.info(f'  Google SERP: {len(all_results)} risultati (deduplicati)')
        return all_results[:num_results]

    except Exception as e:
        logging.error(f'✗ Errore Google: {e}')
        import traceback; logging.error(traceback.format_exc())
        return deduplicate(all_results)[:num_results]


# =============================================================================
# BING SERP (SerpAPI)
# NOTA: Bing spesso non restituisce date affidabili.
# Per monitoraggio cronologico usare RSS.
# =============================================================================

def search_bing(keyword, num_results=30, time_filter=None, sites=None):
    if not SEARCH_ENGINES['bing']['enabled']:
        return []

    serpapi_key = os.getenv('SERPAPI_KEY')
    if not serpapi_key:
        return []

    query = keyword
    if sites:
        query = f'{keyword} ({" OR ".join([f"site:{s.strip()}" for s in sites])})'

    all_results = []
    pages_needed = (num_results + 9) // 10
    empty_pages  = 0

    try:
        bc = SEARCH_ENGINES['bing']
        logging.info(f'🔍 Bing SERP: "{keyword}" (target {num_results})')

        for page in range(pages_needed):
            params = {
                'engine': 'bing', 'q': query,
                'first': page * 10 + 1, 'count': 10,
                'cc': bc['cc'], 'mkt': bc['market'],
                'api_key': serpapi_key,
            }
            if time_filter == 'day':    params['freshness'] = 'Day'
            elif time_filter == 'week': params['freshness'] = 'Week'
            elif time_filter == 'month': params['freshness'] = 'Month'

            resp    = requests.get('https://serpapi.com/search', params=params, timeout=15)
            resp.raise_for_status()
            organic = resp.json().get('organic_results', [])

            if not organic:
                empty_pages += 1
                if empty_pages >= 2: break
                time.sleep(0.5)
                continue

            empty_pages = 0
            for item in organic:
                all_results.append({
                    'position': len(all_results) + 1,
                    'title':    item.get('title',   'N/A') or 'N/A',
                    'url':      item.get('link',    'N/A'),
                    'snippet':  item.get('snippet', '') or '',
                    'date':     item.get('date',    'N/A') or 'N/A',
                    'source':   f"Bing.{bc['cc']}",
                })

            if len(all_results) >= num_results: break
            if page < pages_needed - 1: time.sleep(0.5)

        all_results = deduplicate(all_results)
        for i, r in enumerate(all_results, 1):
            r['position'] = i

        logging.info(f'  Bing SERP: {len(all_results)} risultati (deduplicati)')
        return all_results[:num_results]

    except Exception as e:
        logging.error(f'✗ Errore Bing: {e}')
        import traceback; logging.error(traceback.format_exc())
        return deduplicate(all_results)[:num_results]


# =============================================================================
# GOOGLE IMAGES
# =============================================================================

def search_google_images(keyword, num_results=30, sites=None):
    serpapi_key = os.getenv('SERPAPI_KEY')
    if not serpapi_key:
        return []

    query = keyword
    if sites:
        query = f'{keyword} ({" OR ".join([f"site:{s.strip()}" for s in sites])})'

    try:
        gc = SEARCH_ENGINES['google']
        params = {
            'engine': 'google_images', 'q': query, 'num': num_results,
            'hl': gc['hl'], 'gl': gc['gl'], 'google_domain': gc['domain'],
            'api_key': serpapi_key,
        }
        resp = requests.get('https://serpapi.com/search', params=params, timeout=15)
        resp.raise_for_status()
        images = []
        for idx, item in enumerate(resp.json().get('images_results', [])[:num_results], 1):
            images.append({
                'position': idx, 'title': item.get('title', 'N/A'),
                'link': item.get('link', 'N/A'), 'source': item.get('source', 'N/A'),
                'thumbnail': item.get('thumbnail', ''), 'original': item.get('original', ''),
            })
        logging.info(f'  Immagini: {len(images)} trovate')
        return images
    except Exception as e:
        logging.error(f'✗ Errore Google Images: {e}')
        return []


# =============================================================================
# SALVATAGGIO EXCEL
# Foglio 1: RSS Cronologico   <- menzioni ordinate per data, date reali
# Foglio 2: Google SERP       <- panoramica rilevanza
# Foglio 3: Bing SERP         <- panoramica rilevanza
# Foglio 4: Riepilogo
# Foglio 5: Immagini (opz.)
# =============================================================================

def save_results(google_results, bing_results, rss_results, summary, images=None):
    try:
        logging.info('💾 Salvataggio Excel...')

        with pd.ExcelWriter(EXCEL_FILE, engine='openpyxl') as writer:

            # Foglio 1: RSS Cronologico (primo = piu importante)
            if rss_results:
                df = pd.DataFrame(rss_results)
                cols = [c for c in ['keyword','position','title','source_name','date','url','snippet','timestamp'] if c in df.columns]
                df[cols].to_excel(writer, sheet_name='RSS Cronologico', index=False)
                logging.info(f'  ✓ RSS Cronologico: {len(df)} articoli')

            # Foglio 2: Google SERP
            if google_results:
                df = pd.DataFrame(google_results)
                cols = [c for c in ['keyword','position','title','url','snippet','date','timestamp'] if c in df.columns]
                df[cols].to_excel(writer, sheet_name='Google SERP', index=False)
                logging.info(f'  ✓ Google SERP: {len(df)} risultati')

            # Foglio 3: Bing SERP
            if bing_results:
                df = pd.DataFrame(bing_results)
                cols = [c for c in ['keyword','position','title','url','snippet','date','timestamp'] if c in df.columns]
                df[cols].to_excel(writer, sheet_name='Bing SERP', index=False)
                logging.info(f'  ✓ Bing SERP: {len(df)} risultati')

            # Foglio 4: Riepilogo
            if summary:
                rows = [{
                    'Keyword':             s['Keyword'],
                    'RSS articoli':        s.get('Risultati RSS', 0),
                    'Google SERP':         s.get('Risultati Google', 0),
                    'Bing SERP':           s.get('Risultati Bing', 0),
                    'Timestamp':           s['Timestamp'],
                    'Note RSS':            s.get('rss_note', ''),
                } for s in summary]
                pd.DataFrame(rows).to_excel(writer, sheet_name='Riepilogo', index=False)
                logging.info(f'  ✓ Riepilogo: {len(rows)} keyword')

            # Foglio 5: Immagini
            if images:
                pd.DataFrame(images).to_excel(writer, sheet_name='Immagini', index=False)
                logging.info(f'  ✓ Immagini: {len(images)}')

        logging.info(f'✅ Excel salvato: {EXCEL_FILE}')

    except Exception as e:
        logging.error(f'❌ Errore salvataggio: {e}')
        import traceback; logging.error(traceback.format_exc())


# =============================================================================
# EMAIL
# =============================================================================

def send_email(summary_data, recipients, image_summary=None):
    api_key = os.getenv('MAILGUN_API_KEY')
    domain  = os.getenv('MAILGUN_DOMAIN')
    if not api_key or not domain or not recipients:
        logging.warning('Mailgun non configurato o nessun destinatario — email saltata')
        return

    try:
        recipient_list = [r.strip() for r in recipients.split(',') if r.strip()]
        if not recipient_list: return

        html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>
  body{{font-family:Arial,sans-serif;color:#333}}
  .header{{background:linear-gradient(135deg,#a4404e,#26406b);color:white;padding:30px;text-align:center}}
  .kw{{background:#f8f9fa;padding:15px;margin:15px 0;border-left:4px solid #a4404e}}
  .badge{{display:inline-block;background:#26406b;color:white;border-radius:4px;padding:2px 8px;font-size:.8em;margin-right:4px}}
  .note{{font-size:.85em;color:#555;font-style:italic;margin:6px 0}}
  ol li{{margin-bottom:6px}}
</style></head><body>
<div class="header"><h1>📊 SERP Monitoring Report</h1>
<p>{datetime.now().strftime('%d/%m/%Y %H:%M')}</p></div>
<div style="padding:20px"><h2>Riepilogo</h2>"""

        for item in summary_data:
            html += f'<div class="kw"><h3>🔑 {item["Keyword"]}</h3>'
            html += (f'<span class="badge">📡 RSS: {item.get("Risultati RSS",0)}</span>'
                     f'<span class="badge">🔍 Google: {item.get("Risultati Google",0)}</span>'
                     f'<span class="badge">🔍 Bing: {item.get("Risultati Bing",0)}</span>')
            if item.get('rss_note'):
                html += f'<div class="note">{item["rss_note"]}</div>'

            if item.get('rss_results'):
                html += '<h4>📡 Ultimi articoli (RSS, ordinati per data):</h4><ol>'
                for r in item['rss_results'][:5]:
                    src = r.get('source_name','')
                    html += f'<li><a href="{r["url"]}">{r["title"]}</a>'
                    if src and src != 'N/A': html += f' <em style="color:#888">— {src}</em>'
                    if r.get('date') and r['date'] != 'N/A': html += f' <small>({r["date"]})</small>'
                    html += '</li>'
                html += '</ol>'

            if item.get('google_results'):
                html += '<h4>🔍 Top Google SERP:</h4><ol>'
                for r in item['google_results'][:3]:
                    html += f'<li><a href="{r["url"]}">{r["title"]}</a>'
                    if r.get('date') and r['date'] != 'N/A': html += f' <small>({r["date"]})</small>'
                    html += '</li>'
                html += '</ol>'

            html += '</div>'

        html += '<hr><p><strong>📎 Report completo nel file Excel allegato.</strong></p></div></body></html>'

        url_mg = f'https://api.mailgun.net/v3/{domain}/messages'
        data   = {
            'from': f'SERP Monitor <mailgun@{domain}>',
            'to':   recipient_list,
            'subject': f'SERP Report - {datetime.now().strftime("%d/%m/%Y")}',
            'html': html,
        }
        files = []
        if EXCEL_FILE.exists():
            files = [('attachment', (EXCEL_FILE.name, open(EXCEL_FILE,'rb'),
                      'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'))]

        resp = requests.post(url_mg, auth=('api', api_key), data=data, files=files)
        if files: files[0][1][1].close()
        resp.raise_for_status()
        logging.info(f'✓ Email inviata a {len(recipient_list)} destinatari')

    except Exception as e:
        logging.error(f'✗ Errore email: {e}')
        import traceback; logging.error(traceback.format_exc())


# =============================================================================
# ANALISI PRINCIPALE
# =============================================================================

def run_analysis(keywords, emails, time_filter=None, num_results=30,
                 sites=None, include_images=False,
                 include_rss=True, include_serp=True):
    """
    Due modalita indipendenti:
    📡 RSS  → monitoraggio cronologico (date reali, gratis, ordinato)
    🔍 SERP → panoramica per rilevanza (Google + Bing via SerpAPI)
    """
    global analysis_status

    all_google, all_bing, all_rss, all_images = [], [], [], []
    summary_data  = []
    image_summary = []
    total = len(keywords)

    for idx, keyword in enumerate(keywords, 1):
        analysis_status['current_keyword'] = keyword
        analysis_status['progress'] = int((idx / total) * 100)
        ts = datetime.now().isoformat()

        g_res, b_res, r_res = [], [], []

        # ── RSS ───────────────────────────────────────────────────────────────
        if include_rss:
            r_res = search_rss(keyword, num_results=num_results,
                               time_filter=time_filter, sites=sites)
            for r in r_res:
                r['keyword']   = keyword
                r['timestamp'] = ts
            all_rss.extend(r_res)

            n = len(r_res)
            window = {'day':'24 ore','week':'settimana','month':'mese'}.get(time_filter or '', '')
            if time_filter:
                if   n == 0:           rss_note = f'⚠️ Nessun articolo nelle ultime {window}.'
                elif n < num_results:  rss_note = f'✅ {n} articoli nelle ultime {window} — nessun altro disponibile.'
                else:                  rss_note = f'📋 {n} articoli trovati (limite {num_results}) — potrebbero esservene altri.'
            else:
                rss_note = f'{n} articoli news trovati.'
        else:
            rss_note = ''

        # ── GOOGLE SERP ───────────────────────────────────────────────────────
        if include_serp:
            g_res = search_google(keyword, num_results=num_results,
                                  time_filter=time_filter, sites=sites)
            for r in g_res:
                r['keyword']   = keyword
                r['timestamp'] = ts
            all_google.extend(g_res)

            b_res = search_bing(keyword, num_results=num_results,
                                time_filter=time_filter, sites=sites)
            for r in b_res:
                r['keyword']   = keyword
                r['timestamp'] = ts
            all_bing.extend(b_res)

        # ── IMMAGINI ──────────────────────────────────────────────────────────
        if include_images:
            imgs = search_google_images(keyword, num_results=num_results, sites=sites)
            for img in imgs:
                img['keyword']   = keyword
                img['timestamp'] = ts
            all_images.extend(imgs)
            image_summary.append({'keyword': keyword, 'images': imgs})

        # ── ENTRY RIEPILOGO ───────────────────────────────────────────────────
        entry = {
            'Keyword':          keyword,
            'Risultati RSS':    len(r_res),
            'Risultati Google': len(g_res),
            'Risultati Bing':   len(b_res),
            'Timestamp':        ts,
            'rss_results':      r_res,
            'google_results':   g_res,
            'bing_results':     b_res,
            'rss_note':         rss_note,
        }
        summary_data.append(entry)
        analysis_status['results'].append(entry)

    # ── SALVA ─────────────────────────────────────────────────────────────────
    save_results(
        google_results = all_google if include_serp else [],
        bing_results   = all_bing   if include_serp else [],
        rss_results    = all_rss    if include_rss  else [],
        summary        = summary_data,
        images         = all_images if include_images else None,
    )

    if emails:
        send_email(summary_data, emails, image_summary if include_images else None)

    analysis_status['running']  = False
    analysis_status['progress'] = 100


# =============================================================================
# FLASK ROUTES
# =============================================================================

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('password') == ACCESS_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('index'))
        return render_template('login.html', error='Password errata!')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))


@app.route('/')
@login_required
def index():
    return render_template('index.html')


@app.route('/analyze', methods=['POST'])
@login_required
def analyze():
    global analysis_status
    if analysis_status['running']:
        return jsonify({'error': 'Analisi in corso'}), 400

    data = request.json
    keywords       = data.get('keywords', [])
    emails         = data.get('emails', '')
    time_filter    = data.get('time_filter')
    num_results    = data.get('num_results', 30)
    sites          = data.get('sites', [])
    include_images = data.get('include_images', False)
    include_rss    = data.get('include_rss', True)    # nuovo parametro
    include_serp   = data.get('include_serp', True)   # nuovo parametro

    if not keywords:
        return jsonify({'error': 'Nessuna keyword'}), 400

    analysis_status = {'running': True, 'progress': 0, 'current_keyword': '', 'results': []}
    threading.Thread(
        target=run_analysis,
        args=(keywords, emails, time_filter, num_results, sites,
              include_images, include_rss, include_serp),
        daemon=True
    ).start()
    return jsonify({'status': 'started'})


@app.route('/status')
@login_required
def status():
    return jsonify(analysis_status)


@app.route('/download')
@login_required
def download():
    if EXCEL_FILE.exists():
        return send_file(EXCEL_FILE, as_attachment=True,
                         download_name=f'serp_report_{datetime.now().strftime("%Y%m%d_%H%M")}.xlsx')
    return jsonify({'error': 'File non trovato'}), 404


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)