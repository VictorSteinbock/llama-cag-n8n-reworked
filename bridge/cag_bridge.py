#!/usr/bin/env python3
"""
Simple HTTP server to bridge n8n and llama.cpp CAG
This allows n8n to execute the query_kv_cache.sh script
"""

import os
import subprocess
import json
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("CAG-Bridge")

# Configuration from environment variables
MASTER_KV_CACHE = os.environ.get('MASTER_KV_CACHE', '/data/kv_caches/master_cache.bin')
MODEL_PATH = os.environ.get('LLAMACPP_MODEL_PATH', '/usr/local/llamacpp/models/gemma-4b.gguf')
SCRIPT_PATH = '/usr/local/bin/cag-scripts/query_kv_cache.sh'  # Path to query script
MAX_CONTEXT = os.environ.get('LLAMACPP_MAX_CONTEXT', '128000')
THREADS = os.environ.get('LLAMACPP_THREADS', '4')

# Verify file existence at startup
def check_files():
    issues = []
    
    # Check if the KV cache exists
    if not os.path.exists(MASTER_KV_CACHE):
        issues.append(f"KV cache not found at: {MASTER_KV_CACHE}")
        
    # Check if the model exists
    if not os.path.exists(MODEL_PATH):
        issues.append(f"Model not found at: {MODEL_PATH}")
        
    # Check if the script exists
    if not os.path.exists(SCRIPT_PATH):
        issues.append(f"Script not found at: {SCRIPT_PATH}")
    
    return issues

class CAGHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == '/query':
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length).decode('utf-8')
            
            try:
                # Parse JSON request
                data = json.loads(post_data)
                query = data.get('query', '')
                max_tokens = data.get('maxTokens', 1024)
                temperature = data.get('temperature', 0.7)
                
                # Format the query
                formatted_query = f"Answer this question based on your knowledge:\n\nQuestion: {query}\n\nAnswer:"
                
                # Build command
                temp_param = f"--temp {temperature}" if temperature is not None else ""
                command = f"{SCRIPT_PATH} \"{MODEL_PATH}\" \"{MASTER_KV_CACHE}\" \"{formatted_query}\" {max_tokens} {temp_param}"
                
                logger.info(f"Executing: {command}")
                
                # Execute command
                process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                stdout, stderr = process.communicate()
                
                stdout_text = stdout.decode('utf-8')
                stderr_text = stderr.decode('utf-8')
                
                # Log completion
                logger.info(f"Command completed with exit code: {process.returncode}")
                if stderr_text:
                    logger.warning(f"Command stderr: {stderr_text}")
                
                # Send response
                response = {
                    'success': process.returncode == 0,
                    'response': stdout_text,
                    'error': stderr_text if process.returncode != 0 else None,
                    'query': query
                }
                
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(response).encode('utf-8'))
                
            except Exception as e:
                logger.error(f"Error processing request: {str(e)}", exc_info=True)
                error_response = {'success': False, 'error': str(e)}
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(error_response).encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()
            
    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(b"CAG Bridge Server Running")
        elif self.path == '/health':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            
            # Check for issues
            issues = check_files()
            health_data = {
                'status': 'unhealthy' if issues else 'healthy',
                'issues': issues,
                'config': {
                    'master_kv_cache': MASTER_KV_CACHE,
                    'model_path': MODEL_PATH,
                    'script_path': SCRIPT_PATH,
                    'max_context': MAX_CONTEXT,
                    'threads': THREADS
                }
            }
            self.wfile.write(json.dumps(health_data).encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

def run_server(port=8000):
    server_address = ('', port)
    httpd = HTTPServer(server_address, CAGHandler)
    
    # Check file existence at startup
    issues = check_files()
    if issues:
        for issue in issues:
            logger.warning(issue)
        logger.warning("Bridge started with issues. The service may not work correctly.")
    
    logger.info(f"Starting CAG Bridge Server on port {port}")
    logger.info(f"Using KV cache: {MASTER_KV_CACHE}")
    logger.info(f"Using model: {MODEL_PATH}")
    logger.info(f"Using script: {SCRIPT_PATH}")
    logger.info(f"Context size: {MAX_CONTEXT}")
    logger.info(f"Threads: {THREADS}")
    logger.info("Bridge server is ready to accept requests")
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Server shutting down...")
    finally:
        httpd.server_close()
        logger.info("Server closed")

if __name__ == '__main__':
    run_server(int(os.environ.get('CAG_BRIDGE_PORT', '8000')))