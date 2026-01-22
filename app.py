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
    Google ora restituisce max 10 risultati per chiamata, quindi usiamo 'start' per paginare.
    
    Args:
        keyword: La keyword da cercare
        num_results: Numero totale di risultati desiderati
        time_filter: Filtro temporale (day, week, month)
        sites: Lista di domini per limitare la ricerca (es: ['corriere.it', 'repubblica.it'])
    """
    serpapi_key = os.getenv('SERPAPI_KEY')
    if not serpapi_key:
        logging.error("SERPAPI_KEY non configurata!")
        return []
    
    # Costruisci query con filtro siti se specificato
    query = keyword
    if sites and len(sites) > 0:
        # Crea filtro tipo: (site:corriere.it OR site:repubblica.it OR site:ilpost.it)
        site_filter = ' OR '.join([f'site:{site.strip()}' for site in sites])
        query = f'{keyword} ({site_filter})'
        logging.info(f"   Filtro siti applicato: {len(sites)} domini")
    
    all_results = []
    pages_needed = (num_results + 9) // 10  # Arrotonda per eccesso (30 risultati = 3 pagine)
    
    try:
        logging.info(f"🔍 Google: {keyword} (target {num_results} risultati, {pages_needed} pagine)")
        
        for page in range(pages_needed):
            start = page * 10
            
            params = {
                'engine': 'google',
                'q': query,  # Usa query modificata con site: filter
                'start': start,
                'num': 10,
                'hl': 'it',
                'gl': 'it',
                'api_key': serpapi_key
            }
            
            if time_filter == 'day': params['tbs'] = 'qdr:d'
            elif time_filter == 'week': params['tbs'] = 'qdr:w'
            elif time_filter == 'month': params['tbs'] = 'qdr:m'
            
            response = requests.get('https://serpapi.com/search', params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            
            organic_results = data.get('organic_results', [])
            
            if not organic_results:
                logging.info(f"  Pagina {page+1}: nessun risultato, stop paginazione")
                break
            
            for idx, item in enumerate(organic_results, start + 1):
                all_results.append({
                    'position': idx,
                    'title': item.get('title', 'N/A'),
                    'url': item.get('link', 'N/A'),
                    'snippet': item.get('snippet', ''),
                    'source': 'Google'
                })
            
            logging.info(f"  Pagina {page+1}: +{len(organic_results)} risultati")
            
            # Piccola pausa tra le richieste per non sovraccaricare l'API
            if page < pages_needed - 1:
                time.sleep(0.5)
        
        logging.info(f"✓ Totale {len(all_results)} risultati Google")
        return all_results[:num_results]  # Limita al numero richiesto
        
    except Exception as e:
        logging.error(f"✗ Errore Google: {e}")
        return all_results  # Restituisci quello che hai raccolto finora

def search_bing(keyword, num_results=30, time_filter=None, sites=None):
    """
    Cerca su Bing con paginazione.
    Bing supporta 'offset' per la paginazione.
    
    Args:
        keyword: La keyword da cercare
        num_results: Numero totale di risultati desiderati
        time_filter: Filtro temporale (non supportato da Bing)
        sites: Lista di domini per limitare la ricerca
    """
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
    
    try:
        logging.info(f"🔍 Bing: {keyword} (target {num_results} risultati, {pages_needed} pagine)")
        
        for page in range(pages_needed):
            offset = page * 10
            
            params = {
                'engine': 'bing',
                'q': query,  # Usa query modificata con site: filter
                'first': offset + 1,  # Bing usa 'first' (1-indexed)
                'count': 10,
                'cc': 'it',
                'api_key': serpapi_key
            }
            
            response = requests.get('https://serpapi.com/search', params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            
            organic_results = data.get('organic_results', [])
            
            if not organic_results:
                logging.info(f"  Pagina {page+1}: nessun risultato, stop paginazione")
                break
            
            for idx, item in enumerate(organic_results, offset + 1):
                all_results.append({
                    'position': idx,
                    'title': item.get('title', 'N/A'),
                    'url': item.get('link', 'N/A'),
                    'snippet': item.get('snippet', ''),
                    'source': 'Bing'
                })
            
            logging.info(f"  Pagina {page+1}: +{len(organic_results)} risultati")
            
            if page < pages_needed - 1:
                time.sleep(0.5)
        
        logging.info(f"✓ Totale {len(all_results)} risultati Bing")
        return all_results[:num_results]
        
    except Exception as e:
        logging.error(f"✗ Errore Bing: {e}")
        return all_results

def save_results(results, summary):
    try:
        with pd.ExcelWriter(EXCEL_FILE, engine='openpyxl') as writer:
            # Foglio 1 - Ordinato per Keyword, poi Source (Google prima di Bing), poi Posizione
            if results:
                df = pd.DataFrame(results)
                df = df[['keyword', 'source', 'position', 'title', 'url', 'snippet', 'timestamp']]
                df.columns = ['Keyword', 'Motore', 'Posizione', 'Titolo', 'URL', 'Snippet', 'Timestamp']
                # Ordina: prima per Keyword, poi Google prima di Bing, poi per Posizione
                df['_sort_order'] = df['Motore'].map({'Google': 0, 'Bing': 1})
                df = df.sort_values(['Keyword', '_sort_order', 'Posizione']).drop('_sort_order', axis=1)
            else:
                df = pd.DataFrame(columns=['Keyword', 'Motore', 'Posizione', 'Titolo', 'URL', 'Snippet', 'Timestamp'])
            df.to_excel(writer, sheet_name='Dettaglio SERP', index=False)
            
            # Foglio 2
            if summary:
                df_sum = pd.DataFrame([{
                    'Keyword': s['Keyword'], 'Risultati Google': s['Risultati Google'],
                    'Risultati Bing': s['Risultati Bing'], 'Timestamp': s['Timestamp']
                } for s in summary])
            else:
                df_sum = pd.DataFrame(columns=['Keyword', 'Risultati Google', 'Risultati Bing', 'Timestamp'])
            df_sum.to_excel(writer, sheet_name='Summary', index=False)
            
            # Foglio 3
            total_g = sum(s['Risultati Google'] for s in summary) if summary else 0
            total_b = sum(s['Risultati Bing'] for s in summary) if summary else 0
            avg_pos = sum(r['position'] for r in results) / len(results) if results else 0
            
            df_stats = pd.DataFrame({
                'Metrica': ['Keywords Totali', 'Notizie Google raccolte', 'Notizie Bing raccolte', 
                           'Posizione media SERP', 'Ultima Analisi'],
                'Valore': [len(summary) if summary else 0, total_g, total_b, f"{avg_pos:.1f}", 
                          datetime.now().isoformat()]
            })
            df_stats.to_excel(writer, sheet_name='Statistiche', index=False)
        logging.info("✓ Excel salvato")
    except Exception as e:
        logging.error(f"✗ Errore Excel: {e}")

def send_email(summary, recipients):
    """Invia email via Mailgun API a più destinatari con report e Excel allegato"""
    try:
        api_key = os.getenv('MAILGUN_API_KEY')
        domain = os.getenv('MAILGUN_DOMAIN')
        
        # Parse recipients (può essere stringa con virgole o lista)
        if isinstance(recipients, str):
            recipient_list = [email.strip() for email in recipients.split(',') if email.strip()]
        else:
            recipient_list = recipients
        
        if not recipient_list:
            logging.warning("⚠ Nessun destinatario specificato - skip invio")
            return
        
        logging.info(f"Tentativo invio email via Mailgun API...")
        logging.info(f"API Key configurata: {api_key is not None}")
        logging.info(f"Domain configurato: {domain is not None}")
        logging.info(f"Destinatari: {', '.join(recipient_list)}")
        
        if not all([api_key, domain]):
            logging.warning("⚠ Mailgun non configurato correttamente - skip invio")
            return
        
        # Costruisci HTML email
        html = "<html><body style='font-family: Arial, sans-serif;'>"
        html += "<h2>📊 Report SERP Monitoring</h2>"
        html += f"<p><em>Data: {datetime.now().strftime('%d/%m/%Y alle %H:%M')}</em></p>"
        
        for item in summary:
            keyword = item['Keyword']
            total_google = len(item.get('google_results', []))
            total_bing = len(item.get('bing_results', []))
            
            html += f"<hr><h3>🔑 Keyword: {keyword}</h3>"
            
            # GOOGLE - Numero totale + primi 10 link
            html += f"<p><strong>Google:</strong> {total_google} risultati trovati</p>"
            if total_google > 0:
                html += "<ol>"
                for idx, r in enumerate(item.get('google_results', [])[:10], 1):
                    if r['url'] != 'N/A':
                        html += f'<li><a href="{r["url"]}">{r["title"]}</a></li>'
                html += "</ol>"
                if total_google > 10:
                    html += f"<p><em>... e altri {total_google - 10} risultati (vedi Excel)</em></p>"
            
            # BING - Numero totale + primi 10 link
            html += f"<p><strong>Bing:</strong> {total_bing} risultati trovati</p>"
            if total_bing > 0:
                html += "<ol>"
                for idx, r in enumerate(item.get('bing_results', [])[:10], 1):
                    if r['url'] != 'N/A':
                        html += f'<li><a href="{r["url"]}">{r["title"]}</a></li>'
                html += "</ol>"
                if total_bing > 10:
                    html += f"<p><em>... e altri {total_bing - 10} risultati (vedi Excel)</em></p>"
        
        html += "<hr><p><strong>📎 Report completo con TUTTI i risultati nel file Excel allegato.</strong></p>"
        html += "</body></html>"
        
        # Prepara richiesta Mailgun (invia a tutti i destinatari insieme)
        url = f"https://api.mailgun.net/v3/{domain}/messages"
        
        data = {
            'from': f'SERP Monitor <mailgun@{domain}>',
            'to': recipient_list,  # Lista di email
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

def run_analysis(keywords, emails, time_filter=None, num_results=30, sites=None):
    global analysis_status
    all_results = []
    summary_data = []
    total = len(keywords)
    
    for idx, keyword in enumerate(keywords, 1):
        analysis_status['current_keyword'] = keyword
        analysis_status['progress'] = int((idx / total) * 100)
        
        google_results = search_google(keyword, num_results=num_results, time_filter=time_filter, sites=sites)
        bing_results = search_bing(keyword, num_results=num_results, time_filter=time_filter, sites=sites)
        combined = google_results + bing_results
        
        for r in combined:
            r['keyword'] = keyword
            r['timestamp'] = datetime.now().isoformat()
        
        all_results.extend(combined)
        summary_data.append({
            'Keyword': keyword, 'Risultati Google': len(google_results),
            'Risultati Bing': len(bing_results), 'Timestamp': datetime.now().isoformat(),
            'google_results': google_results, 'bing_results': bing_results
        })
        analysis_status['results'].append(summary_data[-1])
    
    save_results(all_results, summary_data)
    if emails:
        send_email(summary_data, emails)
    
    analysis_status['running'] = False
    analysis_status['progress'] = 100

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
    
    if not keywords:
        return jsonify({'error': 'Nessuna keyword'}), 400
    
    analysis_status = {'running': True, 'progress': 0, 'current_keyword': '', 'results': []}
    thread = threading.Thread(target=run_analysis, args=(keywords, emails, time_filter, num_results, sites))
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