from flask import Flask, render_template, request, jsonify, send_file
import sys
import json
from datetime import datetime
from pathlib import Path
import threading

# Importa le funzioni dal serp_monitor
sys.path.insert(0, str(Path(__file__).parent))
from serp_monitor import (
    search_google, search_bing, analyze_sentiment, 
    calculate_reputation_score, save_results, 
    save_history, send_email_report, EXCEL_FILE,
    NEGATIVE_KEYWORDS, POSITIVE_KEYWORDS
)

app = Flask(__name__)

# Stato dell'analisi in corso
analysis_status = {
    'running': False,
    'progress': 0,
    'current_keyword': '',
    'results': []
}

@app.route('/')
def index():
    """Pagina principale"""
    return render_template('index.html')

@app.route('/analyze', methods=['POST'])
def analyze():
    """Avvia l'analisi"""
    global analysis_status
    
    if analysis_status['running']:
        return jsonify({'error': 'Analisi già in corso'}), 400
    
    data = request.json
    keywords = data.get('keywords', [])
    email = data.get('email', '')
    time_filter = data.get('time_filter', None)  # 'day', 'week', 'month', None
    
    if not keywords:
        return jsonify({'error': 'Nessuna keyword fornita'}), 400
    
    # Resetta lo stato
    analysis_status = {
        'running': True,
        'progress': 0,
        'current_keyword': '',
        'results': []
    }
    
    # Avvia analisi in background
    thread = threading.Thread(
        target=run_analysis,
        args=(keywords, email, time_filter)
    )
    thread.daemon = True
    thread.start()
    
    return jsonify({'status': 'started'})

@app.route('/status')
def status():
    """Ritorna lo stato dell'analisi"""
    return jsonify(analysis_status)

@app.route('/download')
def download():
    """Download del file Excel"""
    if EXCEL_FILE.exists():
        return send_file(
            EXCEL_FILE,
            as_attachment=True,
            download_name=f'serp_report_{datetime.now().strftime("%Y%m%d_%H%M")}.xlsx'
        )
    return jsonify({'error': 'File non trovato'}), 404

def run_analysis(keywords, email, time_filter=None):
    """Esegue l'analisi in background
    
    Args:
        keywords: lista di parole chiave
        email: email per invio report
        time_filter: 'day', 'week', 'month' o None
    """
    global analysis_status
    
    all_results = []
    summary_data = []
    
    total = len(keywords)
    
    for idx, keyword in enumerate(keywords, 1):
        analysis_status['current_keyword'] = keyword
        analysis_status['progress'] = int((idx / total) * 100)
        
        # Cerca su Google con filtro temporale
        google_results = search_google(keyword, time_filter=time_filter)
        
        # Cerca su Bing con filtro temporale
        bing_results = search_bing(keyword, time_filter=time_filter)
        
        # Combina risultati
        combined_results = google_results + bing_results
        
        # Analizza sentiment
        for result in combined_results:
            full_text = f"{result['title']} {result['snippet']}"
            result['sentiment'] = analyze_sentiment(full_text)
            result['keyword'] = keyword
            result['timestamp'] = datetime.now().isoformat()
        
        all_results.extend(combined_results)
        
        # Calcola metriche
        google_only = [r for r in combined_results if r['source'] == 'Google']
        bing_only = [r for r in combined_results if r['source'] == 'Bing']
        
        google_score = calculate_reputation_score(google_only)
        bing_score = calculate_reputation_score(bing_only)
        avg_score = (google_score + bing_score) / 2 if google_only and bing_only else (google_score or bing_score)
        
        status_text = '✓ OK' if avg_score >= 70 else ('⚠ ATTENZIONE' if avg_score >= 40 else '❌ CRITICO')
        
        summary_item = {
            'Keyword': keyword,
            'Risultati Google': len(google_only),
            'Risultati Bing': len(bing_only),
            'Google Score': google_score,
            'Bing Score': bing_score,
            'Score Medio': round(avg_score, 1),
            'Stato': status_text,
            'Timestamp': datetime.now().isoformat(),
            'google_results': google_only,
            'bing_results': bing_only
        }
        
        summary_data.append(summary_item)
        analysis_status['results'].append(summary_item)
    
    # Salva risultati
    save_results(all_results, summary_data)
    save_history(summary_data)
    
    # Invia email se fornita
    if email:
        import os
        os.environ['ALERT_EMAIL'] = email
        send_email_report(summary_data)
    
    analysis_status['running'] = False
    analysis_status['progress'] = 100

if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
