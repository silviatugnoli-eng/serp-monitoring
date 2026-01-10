from flask import Flask, render_template, request, jsonify, send_file
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
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication

# Carica variabili ambiente
load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

app = Flask(__name__)

# Configurazione
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
EXCEL_FILE = DATA_DIR / "serp_monitoring_results.xlsx"
HISTORY_FILE = DATA_DIR / "serp_history.json"

# Headers per evitare blocchi
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7',
}

# Sentiment keywords
NEGATIVE_KEYWORDS = [
    'scandalo', 'critica', 'polemiche', 'accusa', 'condanna', 'fallimento',
    'disastro', 'problema', 'errore', 'bufera', 'caso', 'inchiesta',
]

POSITIVE_KEYWORDS = [
    'successo', 'eccellenza', 'premio', 'vittoria', 'innovazione', 'trionfo',
    'riconoscimento', 'leadership', 'merito', 'onore', 'apprezzamento',
]

# Stato dell'analisi in corso
analysis_status = {
    'running': False,
    'progress': 0,
    'current_keyword': '',
    'results': []
}

def search_google(keyword, num_results=10, time_filter=None):
    """Cerca su Google usando SerpAPI"""
    serpapi_key = os.getenv('SERPAPI_KEY')
    if not serpapi_key:
        logging.error("SERPAPI_KEY non configurata!")
        return []
    
    try:
        logging.info(f"🔍 Cercando su Google: {keyword}")
        
        params = {
            'engine': 'google',
            'q': keyword,
            'num': num_results,
            'hl': 'it',
            'gl': 'it',
            'api_key': serpapi_key
        }
        
        if time_filter == 'day':
            params['tbs'] = 'qdr:d'
        elif time_filter == 'week':
            params['tbs'] = 'qdr:w'
        elif time_filter == 'month':
            params['tbs'] = 'qdr:m'
        
        response = requests.get('https://serpapi.com/search', params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        results = []
        for idx, item in enumerate(data.get('organic_results', [])[:num_results], 1):
            results.append({
                'position': idx,
                'title': item.get('title', 'N/A'),
                'url': item.get('link', 'N/A'),
                'snippet': item.get('snippet', ''),
                'source': 'Google'
            })
        
        logging.info(f"✓ Trovati {len(results)} risultati Google")
        return results
    except Exception as e:
        logging.error(f"✗ Errore Google: {e}")
        return []

def search_bing(keyword, num_results=10, time_filter=None):
    """Cerca su Bing usando SerpAPI"""
    serpapi_key = os.getenv('SERPAPI_KEY')
    if not serpapi_key:
        return []
    
    try:
        logging.info(f"🔍 Cercando su Bing: {keyword}")
        
        params = {
            'engine': 'bing',
            'q': keyword,
            'count': num_results,
            'cc': 'it',
            'api_key': serpapi_key
        }
        
        response = requests.get('https://serpapi.com/search', params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        results = []
        for idx, item in enumerate(data.get('organic_results', [])[:num_results], 1):
            results.append({
                'position': idx,
                'title': item.get('title', 'N/A'),
                'url': item.get('link', 'N/A'),
                'snippet': item.get('snippet', ''),
                'source': 'Bing'
            })
        
        logging.info(f"✓ Trovati {len(results)} risultati Bing")
        return results
    except Exception as e:
        logging.error(f"✗ Errore Bing: {e}")
        return []

def analyze_sentiment(text):
    """Analizza sentiment"""
    text_lower = text.lower()
    negative = sum(1 for w in NEGATIVE_KEYWORDS if w in text_lower)
    positive = sum(1 for w in POSITIVE_KEYWORDS if w in text_lower)
    
    if negative > positive:
        return 'NEGATIVO'
    elif positive > negative:
        return 'POSITIVO'
    return 'NEUTRO'

def save_results(results, summary):
    """Salva risultati in Excel"""
    try:
        with pd.ExcelWriter(EXCEL_FILE, engine='openpyxl') as writer:
            # Foglio 1: Dettaglio SERP
            if results:
                df = pd.DataFrame(results)
                df = df[['keyword', 'source', 'position', 'title', 'url', 'snippet', 'timestamp']]
                df.columns = ['Keyword', 'Motore', 'Posizione', 'Titolo', 'URL', 'Snippet', 'Timestamp']
            else:
                df = pd.DataFrame(columns=['Keyword', 'Motore', 'Posizione', 'Titolo', 'URL', 'Snippet', 'Timestamp'])
            df.to_excel(writer, sheet_name='Dettaglio SERP', index=False)
            
            # Foglio 2: Summary
            if summary:
                df_sum = pd.DataFrame([{
                    'Keyword': s['Keyword'],
                    'Risultati Google': s['Risultati Google'],
                    'Risultati Bing': s['Risultati Bing'],
                    'Timestamp': s['Timestamp']
                } for s in summary])
            else:
                df_sum = pd.DataFrame(columns=['Keyword', 'Risultati Google', 'Risultati Bing', 'Timestamp'])
            df_sum.to_excel(writer, sheet_name='Summary', index=False)
            
            # Foglio 3: Statistiche
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
        
        logging.info(f"✓ Excel salvato")
    except Exception as e:
        logging.error(f"✗ Errore Excel: {e}")

def send_email(summary):
    """Invia email con risultati"""
    try:
        sender = os.getenv('SENDER_EMAIL')
        password = os.getenv('SENDER_PASSWORD')
        recipient = os.getenv('ALERT_EMAIL')
        
        if not all([sender, password, recipient]):
            return
        
        msg = MIMEMultipart()
        msg['From'] = sender
        msg['To'] = recipient
        msg['Subject'] = f"SERP Report - {datetime.now().strftime('%d/%m/%Y')}"
        
        html = "<html><body><h2>Report SERP Monitoring</h2>"
        for item in summary:
            html += f"<p><strong>{item['Keyword']}</strong><br>"
            
            google_results = item.get('google_results', [])
            if google_results:
                html += "<strong>Link Google:</strong><br>"
                for idx, r in enumerate(google_results[:3], 1):
                    if r['url'] != 'N/A':
                        html += f'{idx}. <a href="{r["url"]}">{r["title"]}</a><br>'
            
            bing_results = item.get('bing_results', [])
            if bing_results:
                html += "<strong>Link Bing:</strong><br>"
                for idx, r in enumerate(bing_results[:3], 1):
                    if r['url'] != 'N/A':
                        html += f'{idx}. <a href="{r["url"]}">{r["title"]}</a><br>'
            html += "</p>"
        
        html += "</body></html>"
        msg.attach(MIMEText(html, 'html'))
        
        if EXCEL_FILE.exists():
            with open(EXCEL_FILE, 'rb') as f:
                attach = MIMEApplication(f.read(), _subtype='xlsx')
                attach.add_header('Content-Disposition', 'attachment', filename=EXCEL_FILE.name)
                msg.attach(attach)
        
        with smtplib.SMTP(os.getenv('SMTP_SERVER', 'smtp.gmail.com'), 
                         int(os.getenv('SMTP_PORT', 587))) as server:
            server.starttls()
            server.login(sender, password)
            server.send_message(msg)
        
        logging.info("✓ Email inviata")
    except Exception as e:
        logging.error(f"✗ Errore email: {e}")

def run_analysis(keywords, email, time_filter=None):
    """Esegue analisi in background"""
    global analysis_status
    
    all_results = []
    summary_data = []
    total = len(keywords)
    
    for idx, keyword in enumerate(keywords, 1):
        analysis_status['current_keyword'] = keyword
        analysis_status['progress'] = int((idx / total) * 100)
        
        google_results = search_google(keyword, time_filter=time_filter)
        bing_results = search_bing(keyword, time_filter=time_filter)
        
        combined = google_results + bing_results
        for r in combined:
            r['sentiment'] = analyze_sentiment(f"{r['title']} {r['snippet']}")
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
    
    save_results(all_results, summary_data)
    
    if email:
        os.environ['ALERT_EMAIL'] = email
        send_email(summary_data)
    
    analysis_status['running'] = False
    analysis_status['progress'] = 100

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/analyze', methods=['POST'])
def analyze():
    global analysis_status
    
    if analysis_status['running']:
        return jsonify({'error': 'Analisi in corso'}), 400
    
    data = request.json
    keywords = data.get('keywords', [])
    email = data.get('email', '')
    time_filter = data.get('time_filter')
    
    if not keywords:
        return jsonify({'error': 'Nessuna keyword'}), 400
    
    analysis_status = {'running': True, 'progress': 0, 'current_keyword': '', 'results': []}
    
    thread = threading.Thread(target=run_analysis, args=(keywords, email, time_filter))
    thread.daemon = True
    thread.start()
    
    return jsonify({'status': 'started'})

@app.route('/status')
def status():
    return jsonify(analysis_status)

@app.route('/download')
def download():
    if EXCEL_FILE.exists():
        return send_file(EXCEL_FILE, as_attachment=True, 
                        download_name=f'serp_report_{datetime.now().strftime("%Y%m%d")}.xlsx')
    return jsonify({'error': 'File non trovato'}), 404

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)