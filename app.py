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

# Carica variabili ambiente
load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'chiave-segreta-da-cambiare-in-produzione')

# PASSWORD DI ACCESSO (cambiala!)
ACCESS_PASSWORD = os.environ.get('ACCESS_PASSWORD', 'serp2026')

# Configurazione
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
EXCEL_FILE = DATA_DIR / "serp_monitoring_results.xlsx"

# ============================================
# CONFIGURAZIONE MOTORI DI RICERCA
# Modifica questi valori per cambiare i motori utilizzati
# ============================================
SEARCH_ENGINES = {
    'google': {
        'enabled': True,
        'domain': 'google.it',  # Modifica qui per cambiare dominio (es: google.com, google.fr)
        'gl': 'it',             # Geolocalizzazione (it, us, fr, etc.)
        'hl': 'it'              # Lingua interfaccia (it, en, fr, etc.)
    },
    'bing': {
        'enabled': True,
        'market': 'it-IT',      # Modifica qui per cambiare mercato (it-IT, en-US, fr-FR, etc.)
        'cc': 'it'              # Codice paese (it, us, fr, etc.)
    }
}

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
}

analysis_status = {'running': False, 'progress': 0, 'current_keyword': '', 'results': []}

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def search_google(keyword, num_results=30, time_filter=None, sites=None):
    """
    Cerca su Google con paginazione per ottenere più di 10 risultati.
    Configurabile tramite SEARCH_ENGINES['google']
    
    🔧 FIX: Non si ferma più alla prima pagina vuota
    """
    if not SEARCH_ENGINES['google']['enabled']:
        return []
        
    serpapi_key = os.getenv('SERPAPI_KEY')
    if not serpapi_key:
        logging.error("SERPAPI_KEY non configurata!")
        return []
    
    # Costruisci query con filtro siti se specificato
    query = keyword
    if sites and len(sites) > 0:
        site_filter = ' OR '.join([f'site:{site.strip()}' for site in sites])
        query = f'{keyword} ({site_filter})'
        logging.info(f"   Filtro siti applicato: {len(sites)} domini")
    
    all_results = []
    pages_needed = (num_results + 9) // 10
    empty_pages = 0  # 🔧 Conta pagine vuote consecutive
    
    try:
        google_config = SEARCH_ENGINES['google']
        logging.info(f"🔍 Google.{google_config['gl']}: {keyword} (target {num_results} risultati, {pages_needed} pagine)")
        
        for page in range(pages_needed):
            start = page * 10
            
            params = {
                'engine': 'google',
                'q': query,
                'start': start,
                'num': 10,
                'hl': google_config['hl'],
                'gl': google_config['gl'],
                'google_domain': google_config['domain'],
                'api_key': serpapi_key
            }
            
            if time_filter == 'day': params['tbs'] = 'qdr:d'
            elif time_filter == 'week': params['tbs'] = 'qdr:w'
            elif time_filter == 'month': params['tbs'] = 'qdr:m'
            
            response = requests.get('https://serpapi.com/search', params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            
            organic_results = data.get('organic_results', [])
            
            # 🔧 FIX: Non fermarti alla prima pagina vuota
            if not organic_results:
                empty_pages += 1
                logging.warning(f"  Pagina {page+1}: nessun risultato (pagine vuote consecutive: {empty_pages})")
                
                # Fermati solo dopo 2 pagine vuote consecutive
                if empty_pages >= 2:
                    logging.info(f"  Stop: {empty_pages} pagine vuote consecutive")
                    break
                
                # Continua a cercare nella prossima pagina
                if page < pages_needed - 1:
                    time.sleep(0.5)
                continue
            
            # Reset contatore se troviamo risultati
            empty_pages = 0
            
            for idx, item in enumerate(organic_results, start + 1):
                pub_date = item.get('date', '')
                if not pub_date:
                    pub_date = item.get('snippet_highlighted_words', {}).get('date', '')
                
                all_results.append({
                    'position': idx,
                    'title': item.get('title', 'N/A'),
                    'url': item.get('link', 'N/A'),
                    'snippet': item.get('snippet', ''),
                    'date': pub_date if pub_date else 'N/A',
                    'source': f"Google.{google_config['gl']}"
                })
            
            logging.info(f"  Pagina {page+1}: +{len(organic_results)} risultati (totale: {len(all_results)})")
            
            # 🔧 Fermati se abbiamo raggiunto il numero richiesto
            if len(all_results) >= num_results:
                logging.info(f"  ✓ Raggiunto target di {num_results} risultati")
                break
            
            if page < pages_needed - 1:
                time.sleep(0.5)
        
        logging.info(f"✓ Totale {len(all_results)} risultati Google")
        return all_results[:num_results]
        
    except Exception as e:
        logging.error(f"✗ Errore Google: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return all_results

def search_bing(keyword, num_results=30, time_filter=None, sites=None):
    """
    Cerca su Bing con paginazione.
    Configurabile tramite SEARCH_ENGINES['bing']
    
    🔧 FIX: Non si ferma più alla prima pagina vuota
    """
    if not SEARCH_ENGINES['bing']['enabled']:
        return []
        
    serpapi_key = os.getenv('SERPAPI_KEY')
    if not serpapi_key:
        return []
    
    # Costruisci query con filtro siti se specificato
    query = keyword
    if sites and len(sites) > 0:
        site_filter = ' OR '.join([f'site:{site.strip()}' for site in sites])
        query = f'{keyword} ({site_filter})'
        logging.info(f"   Filtro siti applicato: {len(sites)} domini")
    
    all_results = []
    pages_needed = (num_results + 9) // 10
    empty_pages = 0  # 🔧 Conta pagine vuote consecutive
    
    try:
        bing_config = SEARCH_ENGINES['bing']
        logging.info(f"🔍 Bing.{bing_config['cc']}: {keyword} (target {num_results} risultati, {pages_needed} pagine)")
        
        for page in range(pages_needed):
            offset = page * 10
            
            params = {
                'engine': 'bing',
                'q': query,
                'first': offset + 1,
                'count': 10,
                'cc': bing_config['cc'],
                'mkt': bing_config['market'],
                'api_key': serpapi_key
            }
            
            response = requests.get('https://serpapi.com/search', params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            
            organic_results = data.get('organic_results', [])
            
            # 🔧 FIX: Non fermarti alla prima pagina vuota
            if not organic_results:
                empty_pages += 1
                logging.warning(f"  Pagina {page+1}: nessun risultato (pagine vuote consecutive: {empty_pages})")
                
                # Fermati solo dopo 2 pagine vuote consecutive
                if empty_pages >= 2:
                    logging.info(f"  Stop: {empty_pages} pagine vuote consecutive")
                    break
                
                # Continua a cercare nella prossima pagina
                if page < pages_needed - 1:
                    time.sleep(0.5)
                continue
            
            # Reset contatore se troviamo risultati
            empty_pages = 0
            
            for idx, item in enumerate(organic_results, offset + 1):
                pub_date = item.get('date', '')
                if not pub_date:
                    pub_date = item.get('snippet_highlighted_words', {}).get('date', '')
                
                all_results.append({
                    'position': idx,
                    'title': item.get('title', 'N/A'),
                    'url': item.get('link', 'N/A'),
                    'snippet': item.get('snippet', ''),
                    'date': pub_date if pub_date else 'N/A',
                    'source': f"Bing.{bing_config['cc']}"
                })
            
            logging.info(f"  Pagina {page+1}: +{len(organic_results)} risultati (totale: {len(all_results)})")
            
            # 🔧 Fermati se abbiamo raggiunto il numero richiesto
            if len(all_results) >= num_results:
                logging.info(f"  ✓ Raggiunto target di {num_results} risultati")
                break
            
            if page < pages_needed - 1:
                time.sleep(0.5)
        
        logging.info(f"✓ Totale {len(all_results)} risultati Bing")
        return all_results[:num_results]
        
    except Exception as e:
        logging.error(f"✗ Errore Bing: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return all_results

def search_google_news(keyword, num_results=10, time_filter=None, sites=None):
    """
    Cerca nelle Google News (Notizie principali) con paginazione.
    Restituisce le notizie più recenti per la keyword.
    
    Args:
        keyword: La keyword da cercare
        num_results: Numero totale di notizie desiderate
        time_filter: Filtro temporale (day, week, month)
        sites: Lista di domini per limitare la ricerca
    """
    if not SEARCH_ENGINES['google']['enabled']:
        return []
        
    serpapi_key = os.getenv('SERPAPI_KEY')
    if not serpapi_key:
        logging.error("SERPAPI_KEY non configurata!")
        return []
    
    # Costruisci query con filtro siti se specificato
    query = keyword
    if sites and len(sites) > 0:
        site_filter = ' OR '.join([f'site:{site.strip()}' for site in sites])
        query = f'{keyword} ({site_filter})'
        logging.info(f"   Filtro siti applicato: {len(sites)} domini")
    
    all_news = []
    pages_needed = (num_results + 9) // 10  # Google News restituisce ~10 risultati per pagina
    
    try:
        google_config = SEARCH_ENGINES['google']
        logging.info(f"📰 Google News ({google_config['gl']}): {keyword} (target {num_results} notizie, {pages_needed} pagine)")
        
        for page in range(pages_needed):
            start = page * 10
            
            params = {
                'engine': 'google',
                'q': query,
                'tbm': 'nws',  # ⭐ Ricerca news
                'start': start,
                'num': 10,
                'hl': google_config['hl'],
                'gl': google_config['gl'],
                'google_domain': google_config['domain'],
                'api_key': serpapi_key
            }
            
            # Applica filtro temporale se specificato
            if time_filter == 'day': 
                params['tbs'] = 'qdr:d'
                logging.info(f"   Filtro temporale: ultime 24 ore")
            elif time_filter == 'week': 
                params['tbs'] = 'qdr:w'
                logging.info(f"   Filtro temporale: ultima settimana")
            elif time_filter == 'month': 
                params['tbs'] = 'qdr:m'
                logging.info(f"   Filtro temporale: ultimo mese")
            
            response = requests.get('https://serpapi.com/search', params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            
            news_results = data.get('news_results', [])
            
            if not news_results:
                logging.info(f"  Pagina {page+1}: nessuna notizia, stop paginazione")
                break
            
            for idx, item in enumerate(news_results, start + 1):
                # Estrai informazioni dalla news
                source = item.get('source', {})
                source_name = source.get('name', 'N/A') if isinstance(source, dict) else 'N/A'
                date = item.get('date', 'N/A')
                
                all_news.append({
                    'position': idx,
                    'title': item.get('title', 'N/A'),
                    'url': item.get('link', 'N/A'),
                    'snippet': item.get('snippet', ''),
                    'source_name': source_name,
                    'date': date,
                    'thumbnail': item.get('thumbnail', ''),
                    'type': 'Google News'
                })
            
            logging.info(f"  Pagina {page+1}: +{len(news_results)} notizie")
            
            # Pausa tra le richieste
            if page < pages_needed - 1:
                time.sleep(0.5)
        
        logging.info(f"✓ Totale {len(all_news)} notizie")
        return all_news[:num_results]  # Limita al numero richiesto
        
    except Exception as e:
        logging.error(f"✗ Errore Google News: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return []

def search_google_images(keyword, num_results=30, sites=None):
    """Cerca immagini su Google"""
    if not SEARCH_ENGINES['google']['enabled']:
        return []
        
    serpapi_key = os.getenv('SERPAPI_KEY')
    if not serpapi_key:
        return []
    
    query = keyword
    if sites and len(sites) > 0:
        site_filter = ' OR '.join([f'site:{site.strip()}' for site in sites])
        query = f'{keyword} ({site_filter})'
    
    try:
        google_config = SEARCH_ENGINES['google']
        logging.info(f"🖼️  Google Images ({google_config['gl']}): {keyword}")
        
        params = {
            'engine': 'google_images',
            'q': query,
            'num': num_results,
            'hl': google_config['hl'],
            'gl': google_config['gl'],
            'google_domain': google_config['domain'],
            'api_key': serpapi_key
        }
        
        response = requests.get('https://serpapi.com/search', params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        images = []
        for idx, item in enumerate(data.get('images_results', [])[:num_results], 1):
            images.append({
                'position': idx,
                'title': item.get('title', 'N/A'),
                'link': item.get('link', 'N/A'),
                'source': item.get('source', 'N/A'),
                'thumbnail': item.get('thumbnail', ''),
                'original': item.get('original', ''),
            })
        
        logging.info(f"✓ Trovate {len(images)} immagini")
        return images
        
    except Exception as e:
        logging.error(f"✗ Errore Google Images: {e}")
        return []

def save_results(results, summary, images=None, news=None):
    """
    Salva risultati in Excel con fogli separati per Google, Bing e News
    
    🔧 FIX: Corretto il salvataggio del foglio Google News
    """
    try:
        logging.info(f"💾 Salvataggio risultati in Excel...")
        
        with pd.ExcelWriter(EXCEL_FILE, engine='openpyxl') as writer:
            # Separa i risultati per fonte (solo risultati organici)
            if results:
                df_all = pd.DataFrame(results)
                
                # Foglio Google (solo risultati organici)
                google_results = df_all[df_all['source'].str.contains('Google', na=False)]
                if not google_results.empty:
                    google_results = google_results[['keyword', 'position', 'title', 'url', 'snippet', 'date', 'timestamp']]
                    google_results.to_excel(writer, sheet_name='Google', index=False)
                    logging.info(f"  ✓ Foglio Google: {len(google_results)} risultati")
                
                # Foglio Bing
                bing_results = df_all[df_all['source'].str.contains('Bing', na=False)]
                if not bing_results.empty:
                    bing_results = bing_results[['keyword', 'position', 'title', 'url', 'snippet', 'date', 'timestamp']]
                    bing_results.to_excel(writer, sheet_name='Bing', index=False)
                    logging.info(f"  ✓ Foglio Bing: {len(bing_results)} risultati")
            
            # 🔧 FIX: Foglio Google News (CORRETTO)
            # Le news hanno una struttura diversa, non vanno filtrate da 'results'
            if news and len(news) > 0:
                df_news = pd.DataFrame(news)
                # Seleziona solo le colonne rilevanti per le news
                news_columns = ['keyword', 'position', 'title', 'url', 'snippet', 'source_name', 'date', 'timestamp']
                df_news = df_news[news_columns]
                df_news.to_excel(writer, sheet_name='Google News', index=False)
                logging.info(f"  ✓ Foglio Google News: {len(df_news)} notizie")
            
            # Foglio riassunto
            if summary:
                summary_df = pd.DataFrame([{
                    'Keyword': s['Keyword'],
                    'Risultati Google': s['Risultati Google'],
                    'Risultati Bing': s['Risultati Bing'],
                    'Timestamp': s['Timestamp']
                } for s in summary])
                summary_df.to_excel(writer, sheet_name='Riepilogo', index=False)
                logging.info(f"  ✓ Foglio Riepilogo: {len(summary_df)} keywords")
            
            # Foglio immagini (se richiesto)
            if images and len(images) > 0:
                df_images = pd.DataFrame(images)
                df_images.to_excel(writer, sheet_name='Immagini', index=False)
                logging.info(f"  ✓ Foglio Immagini: {len(df_images)} immagini")
        
        logging.info(f"✅ Risultati salvati con successo in {EXCEL_FILE}")
        
    except Exception as e:
        logging.error(f"❌ Errore salvataggio Excel: {e}")
        import traceback
        logging.error(traceback.format_exc())

def send_email(summary_data, recipients, image_summary=None, news_summary=None):
    """Invia email con report via Mailgun"""
    
    api_key = os.getenv('MAILGUN_API_KEY')
    domain = os.getenv('MAILGUN_DOMAIN')
    
    if not api_key or not domain:
        logging.warning("Mailgun non configurato - email non inviata")
        return
    
    if not recipients:
        logging.warning("Nessun destinatario specificato")
        return
    
    try:
        recipient_list = [r.strip() for r in recipients.split(',') if r.strip()]
        if not recipient_list:
            return
        
        # Costruisci HTML email
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; }
                .header { background: linear-gradient(135deg, #a4404e 0%, #26406b 100%); 
                         color: white; padding: 30px; text-align: center; }
                .content { padding: 20px; }
                .keyword { background: #f8f9fa; padding: 15px; margin: 15px 0; 
                          border-left: 4px solid #a4404e; }
                .stats { display: flex; gap: 20px; margin-top: 10px; }
                .stat { background: white; padding: 10px; border-radius: 5px; }
            </style>
        </head>
        <body>
            <div class="header">
                <h1>📊 SERP Monitoring Report</h1>
                <p>""" + datetime.now().strftime('%d/%m/%Y %H:%M') + """</p>
            </div>
            <div class="content">
                <h2>Riepilogo Analisi</h2>
        """
        
        # Aggiungi risultati per keyword
        for item in summary_data:
            html += f"""
                <div class="keyword">
                    <h3>🔑 {item['Keyword']}</h3>
                    <div class="stats">
                        <div class="stat">
                            <strong>Google:</strong> {item['Risultati Google']} risultati
                        </div>
                        <div class="stat">
                            <strong>Bing:</strong> {item['Risultati Bing']} risultati
                        </div>
                    </div>
            """
            
            # Top 3 risultati Google
            if item.get('google_results') and len(item['google_results']) > 0:
                html += "<h4 style='margin-top: 15px;'>Top 3 Google:</h4><ol>"
                for r in item['google_results'][:3]:
                    html += f"<li><a href='{r['url']}'>{r['title']}</a>"
                    if r.get('date') and r['date'] != 'N/A':
                        html += f" <em style='color: #666;'>({r['date']})</em>"
                    html += "</li>"
                html += "</ol>"
            
            # Top 3 risultati Bing
            if item.get('bing_results') and len(item['bing_results']) > 0:
                html += "<h4 style='margin-top: 15px;'>Top 3 Bing:</h4><ol>"
                for r in item['bing_results'][:3]:
                    html += f"<li><a href='{r['url']}'>{r['title']}</a>"
                    if r.get('date') and r['date'] != 'N/A':
                        html += f" <em style='color: #666;'>({r['date']})</em>"
                    html += "</li>"
                html += "</ol>"
            
            html += "</div>"
        
        # Aggiungi news se presenti
        if news_summary and len(news_summary) > 0:
            html += "<hr><h2>📰 Ultime Notizie</h2>"
            for news_item in news_summary:
                if news_item.get('news') and len(news_item['news']) > 0:
                    html += f"<div class='keyword'><h3>🔑 {news_item['keyword']}</h3><ul>"
                    for news in news_item['news'][:5]:  # Top 5 news
                        html += f"""
                            <li>
                                <a href='{news['url']}'>{news['title']}</a><br>
                                <small style='color: #666;'>{news['source_name']} - {news['date']}</small>
                            </li>
                        """
                    html += "</ul></div>"
        
        # Aggiungi immagini se presenti
        if image_summary and len(image_summary) > 0:
            html += "<hr><h2>🖼️ Immagini trovate</h2>"
            for img_item in image_summary:
                if img_item.get('images') and len(img_item['images']) > 0:
                    html += f"<div class='keyword'><h3>🔑 {img_item['keyword']}</h3>"
                    html += f"<p>Trovate {len(img_item['images'])} immagini</p></div>"
        
        html += "<hr><p><strong>📎 Report completo con TUTTI i risultati nel file Excel allegato.</strong></p>"
        html += "</body></html>"
        
        # Prepara richiesta Mailgun
        url = f"https://api.mailgun.net/v3/{domain}/messages"
        
        data = {
            'from': f'SERP Monitor <mailgun@{domain}>',
            'to': recipient_list,
            'subject': f"SERP Report - {datetime.now().strftime('%d/%m/%Y')}",
            'html': html
        }
        
        files = []
        if EXCEL_FILE.exists():
            files = [('attachment', (EXCEL_FILE.name, open(EXCEL_FILE, 'rb'), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'))]
            logging.info("Excel allegato aggiunto")
        
        # Invia via Mailgun API
        logging.info(f"Invio email a {len(recipient_list)} destinatari via Mailgun...")
        response = requests.post(
            url,
            auth=('api', api_key),
            data=data,
            files=files
        )
        
        # Chiudi file se aperto
        if files:
            files[0][1][1].close()
        
        response.raise_for_status()
        logging.info(f"✓ Email inviata con successo! Response: {response.json()}")
        
    except Exception as e:
        logging.error(f"✗ Errore invio email: {e}")
        import traceback
        logging.error(traceback.format_exc())

def run_analysis(keywords, emails, time_filter=None, num_results=30, sites=None, include_images=False, include_news=False):
    """
    🔧 FIX: Corretto il passaggio delle news alla funzione save_results
    """
    global analysis_status
    all_results = []
    all_images = []
    all_news = []  # ⭐ Lista separata per le news
    summary_data = []
    image_summary = []
    news_summary = []
    total = len(keywords)
    
    for idx, keyword in enumerate(keywords, 1):
        analysis_status['current_keyword'] = keyword
        analysis_status['progress'] = int((idx / total) * 100)
        
        # Ricerca organica Google e Bing
        google_results = search_google(keyword, num_results=num_results, time_filter=time_filter, sites=sites)
        bing_results = search_bing(keyword, num_results=num_results, time_filter=time_filter, sites=sites)
        combined = google_results + bing_results
        
        for r in combined:
            r['keyword'] = keyword
            r['timestamp'] = datetime.now().isoformat()
        
        all_results.extend(combined)
        summary_data.append({
            'Keyword': keyword, 
            'Risultati Google': len(google_results),
            'Risultati Bing': len(bing_results), 
            'Timestamp': datetime.now().isoformat(),
            'google_results': google_results, 
            'bing_results': bing_results
        })
        analysis_status['results'].append(summary_data[-1])
        
        # Cerca immagini se richiesto
        if include_images:
            image_results = search_google_images(keyword, num_results=num_results, sites=sites)
            for img in image_results:
                img['keyword'] = keyword
                img['timestamp'] = datetime.now().isoformat()
            all_images.extend(image_results)
            image_summary.append({
                'keyword': keyword,
                'images': image_results
            })
        
        # 🔧 FIX: Cerca news se richiesto
        if include_news:
            logging.info(f"📰 Cercando news per: {keyword}")
            news_results = search_google_news(keyword, num_results=num_results, time_filter=time_filter, sites=sites)
            
            # Aggiungi keyword e timestamp a ogni news
            for news in news_results:
                news['keyword'] = keyword
                news['timestamp'] = datetime.now().isoformat()
            
            all_news.extend(news_results)  # ⭐ Aggiungi alla lista separata delle news
            news_summary.append({
                'keyword': keyword,
                'news': news_results
            })
            logging.info(f"  ✓ Trovate {len(news_results)} news per '{keyword}'")
    
    # 🔧 FIX: Passa all_news come parametro separato (NON dentro all_results)
    save_results(
        all_results,  # Solo risultati organici Google/Bing
        summary_data, 
        all_images if include_images else None, 
        all_news if include_news else None  # ⭐ Passa le news separatamente
    )
    
    if emails:
        send_email(summary_data, emails, image_summary if include_images else None, news_summary if include_news else None)
    
    analysis_status['running'] = False
    analysis_status['progress'] = 100
    
    # Aggiungi news allo status per mostrarle nell'interfaccia
    if include_news and len(news_summary) > 0:
        analysis_status['news_results'] = news_summary

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
    keywords = data.get('keywords', [])
    emails = data.get('emails', '')
    time_filter = data.get('time_filter')
    num_results = data.get('num_results', 30)
    sites = data.get('sites', [])
    include_images = data.get('include_images', False)
    include_news = data.get('include_news', False)
    
    if not keywords:
        return jsonify({'error': 'Nessuna keyword'}), 400
    
    analysis_status = {'running': True, 'progress': 0, 'current_keyword': '', 'results': []}
    thread = threading.Thread(target=run_analysis, args=(keywords, emails, time_filter, num_results, sites, include_images, include_news))
    thread.daemon = True
    thread.start()
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
                        download_name=f'serp_report_{datetime.now().strftime("%Y%m%d")}.xlsx')
    return jsonify({'error': 'File non trovato'}), 404

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)