from datetime import datetime, timedelta
import functools
import io
import json
import logging
import os
import tempfile
import uuid
import mimetypes
import zipfile
import xml.etree.ElementTree as ET
import shutil
import subprocess
import re
from werkzeug.utils import secure_filename
from flask import Flask, request, jsonify, send_file
import firebase_admin
from firebase_admin import credentials, storage
import magic
from cachetools import TTLCache, cached
from concurrent.futures import ThreadPoolExecutor
import multiprocessing
from lxml import etree

# Optional text extraction libraries
try:
    from PyPDF2 import PdfReader
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False

try:
    from PIL import Image
    from PIL.ExifTags import TAGS
    IMAGE_SUPPORT = True
except ImportError:
    IMAGE_SUPPORT = False

try:
    import docx
    DOCX_SUPPORT = True
except ImportError:
    DOCX_SUPPORT = False

try:
    import openpyxl
    EXCEL_SUPPORT = True
except ImportError:
    EXCEL_SUPPORT = False

# Apple iWork formats support
APPLE_FORMATS_SUPPORT = True

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)

# Set maximum content length (100 MB)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024

# Register Apple file MIME types
mimetypes.add_type('application/vnd.apple.pages', '.pages')
mimetypes.add_type('application/vnd.apple.numbers', '.numbers')
mimetypes.add_type('application/vnd.apple.keynote', '.keynote')

# Initialize high-performance caches
CACHE_TTL = 3600  # 1 hour cache
MAX_CACHE_SIZE = 1000
bucket_cache = {}
results_cache = TTLCache(maxsize=MAX_CACHE_SIZE, ttl=CACHE_TTL)
metadata_cache = TTLCache(maxsize=MAX_CACHE_SIZE, ttl=CACHE_TTL)

# Initialize thread pool for parallel operations
NUM_WORKERS = multiprocessing.cpu_count() * 2
thread_pool = ThreadPoolExecutor(max_workers=NUM_WORKERS)

def api_error_handler(f):
    """Decorator to standardize error handling across API endpoints"""
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            logger.error(f"Error in {f.__name__}: {str(e)}")
            return jsonify({"error": str(e)}), 500
    return decorated_function

def get_bucket():
    """Get the Firebase Storage bucket with caching"""
    if 'default' not in bucket_cache:
        bucket_name = os.environ.get('FIREBASE_STORAGE_BUCKET', 'jamesmemorysync.appspot.com')
        bucket_cache['default'] = storage.bucket(bucket_name)
    return bucket_cache['default']

def initialize_firebase():
    """Initialize Firebase with proper error handling and environment variables"""
    if not firebase_admin._apps:
        try:
            # Get environment variables with fallbacks
            project_id = os.environ.get('PROJECT_ID', 'jamesmemorysync')
            
            logger.info(f"Initializing Firebase Storage with Project ID: {project_id}")
            
            # Attempt to load service account credentials from environment variable
            service_account_json = os.environ.get('FIREBASE_SERVICE_ACCOUNT')
            if service_account_json:
                logger.info("Loading service account credentials from environment variable")
                service_account_info = json.loads(service_account_json)
                cred = credentials.Certificate(service_account_info)
            else:
                # Fallback to local file
                service_account_path = os.path.abspath('jamesmemorysync-firebase-adminsdk-fbsvc-d142d44489.json')
                if not os.path.exists(service_account_path):
                    logger.error(f"Service account file not found at {service_account_path}")
                    files = os.listdir('.')
                    logger.info(f"Directory contents: {files}")
                    raise FileNotFoundError(f"Service account file not found at {service_account_path}")
                    
                logger.info(f"Loading service account credentials from {service_account_path}")
                cred = credentials.Certificate(service_account_path)
            
            firebase_admin.initialize_app(cred, {
                'storageBucket': os.environ.get('FIREBASE_STORAGE_BUCKET', 'jamesmemorysync.appspot.com'),
                'projectId': project_id
            })
            logger.info("Firebase initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Firebase: {str(e)}")
            raise

# Initialize Firebase before any requests
initialize_firebase()

def extract_text_from_apple_iwork(file_path, mime_type):
    """Extract text from Apple iWork files (Pages, Numbers, Keynote)"""
    text_content = ""
    temp_dir = None
    
    try:
        # Create a temp directory for extracting the package
        temp_dir = tempfile.mkdtemp()
        
        # iWork files are essentially zip files
        with zipfile.ZipFile(file_path, 'r') as zip_ref:
            zip_ref.extractall(temp_dir)
            
        if mime_type == 'application/vnd.apple.pages' or 'pages' in file_path.lower():
            # For Pages documents
            index_file = os.path.join(temp_dir, 'index.xml')
            if not os.path.exists(index_file):
                # Try the newer format structure
                index_file = os.path.join(temp_dir, 'Index', 'Document.iwa')
                if os.path.exists(index_file):
                    # Newer format requires snappy decompression, try to extract using textutil if available
                    try:
                        # On macOS, we can use textutil
                        result = subprocess.run(['textutil', '-convert', 'txt', '-stdout', file_path], 
                                               capture_output=True, text=True, check=False)
                        if result.returncode == 0:
                            text_content = result.stdout
                    except (subprocess.SubprocessError, FileNotFoundError):
                        # Fallback to extracting from preview.pdf if it exists
                        preview_pdf = os.path.join(temp_dir, 'QuickLook', 'Preview.pdf')
                        if os.path.exists(preview_pdf) and PDF_SUPPORT:
                            with open(preview_pdf, 'rb') as f:
                                pdf = PdfReader(f)
                                for page in pdf.pages:
                                    text_content += page.extract_text() + "\n"
            else:
                # Extract from index.xml for older format
                tree = ET.parse(index_file)
                root = tree.getroot()
                # Extract text from various elements - structure varies by version
                for elem in root.iter():
                    if elem.text and elem.text.strip():
                        text_content += elem.text.strip() + "\n"
        
        elif mime_type == 'application/vnd.apple.numbers' or 'numbers' in file_path.lower():
            # Similar approach for Numbers
            try:
                # Try textutil first (macOS)
                result = subprocess.run(['textutil', '-convert', 'txt', '-stdout', file_path], 
                                       capture_output=True, text=True, check=False)
                if result.returncode == 0:
                    text_content = result.stdout
            except (subprocess.SubprocessError, FileNotFoundError):
                # Fallback to preview.pdf
                preview_pdf = os.path.join(temp_dir, 'QuickLook', 'Preview.pdf')
                if os.path.exists(preview_pdf) and PDF_SUPPORT:
                    with open(preview_pdf, 'rb') as f:
                        pdf = PdfReader(f)
                        for page in pdf.pages:
                            text_content += page.extract_text() + "\n"
        
        elif mime_type == 'application/vnd.apple.keynote' or 'keynote' in file_path.lower():
            # Similar approach for Keynote
            try:
                # Try textutil first (macOS)
                result = subprocess.run(['textutil', '-convert', 'txt', '-stdout', file_path], 
                                       capture_output=True, text=True, check=False)
                if result.returncode == 0:
                    text_content = result.stdout
            except (subprocess.SubprocessError, FileNotFoundError):
                # Fallback to looking for notes.apxl and presentation.apxl
                notes_file = os.path.join(temp_dir, 'notes.apxl')
                if os.path.exists(notes_file):
                    tree = ET.parse(notes_file)
                    root = tree.getroot()
                    for elem in root.iter():
                        if elem.text and elem.text.strip():
                            text_content += elem.text.strip() + "\n"
                
                # Try extracting from the preview PDF
                preview_pdf = os.path.join(temp_dir, 'QuickLook', 'Preview.pdf')
                if os.path.exists(preview_pdf) and PDF_SUPPORT:
                    with open(preview_pdf, 'rb') as f:
                        pdf = PdfReader(f)
                        for page in pdf.pages:
                            text_content += page.extract_text() + "\n"
                            
    except Exception as e:
        logger.warning(f"Failed to extract text from Apple iWork file {file_path}: {e}")
    
    finally:
        # Clean up the temp directory
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
    
    return text_content

def extract_text_from_file(file_path, mime_type):
    """Extract text content from various file types when possible"""
    text_content = ""
    
    try:
        # Text files
        if mime_type.startswith('text/'):
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                text_content = f.read()
                
        # PDF files
        elif mime_type == 'application/pdf' and PDF_SUPPORT:
            with open(file_path, 'rb') as f:
                pdf = PdfReader(f)
                text_content = ""
                for page in pdf.pages:
                    text_content += page.extract_text() + "\n"
                    
        # Word documents
        elif mime_type == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' and DOCX_SUPPORT:
            doc = docx.Document(file_path)
            text_content = "\n".join([para.text for para in doc.paragraphs])
            
        # Excel files
        elif mime_type in ['application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'application/vnd.ms-excel'] and EXCEL_SUPPORT:
            workbook = openpyxl.load_workbook(file_path)
            text_content = ""
            for sheet in workbook:
                for row in sheet.iter_rows(values_only=True):
                    text_content += " | ".join([str(cell) for cell in row if cell]) + "\n"
        
        # Apple Pages
        elif (mime_type == 'application/vnd.apple.pages' or 
              file_path.lower().endswith('.pages')) and APPLE_FORMATS_SUPPORT:
            text_content = extract_text_from_apple_iwork(file_path, 'application/vnd.apple.pages')
            
        # Apple Numbers
        elif (mime_type == 'application/vnd.apple.numbers' or 
              file_path.lower().endswith('.numbers')) and APPLE_FORMATS_SUPPORT:
            text_content = extract_text_from_apple_iwork(file_path, 'application/vnd.apple.numbers')
            
        # Apple Keynote
        elif (mime_type == 'application/vnd.apple.keynote' or 
              file_path.lower().endswith('.keynote')) and APPLE_FORMATS_SUPPORT:
            text_content = extract_text_from_apple_iwork(file_path, 'application/vnd.apple.keynote')
            
    except Exception as e:
        logger.warning(f"Failed to extract text from {file_path}: {e}")
    
    return text_content

def extract_image_metadata(file_path):
    """Extract metadata from image files when possible"""
    metadata = {}
    
    if not IMAGE_SUPPORT:
        return metadata
        
    try:
        with Image.open(file_path) as img:
            # Basic image properties
            metadata = {
                "format": img.format,
                "mode": img.mode,
                "size": img.size,
            }
            
            # Extract EXIF data if available
            if hasattr(img, '_getexif') and img._getexif():
                exif_data = {}
                for tag_id, value in img._getexif().items():
                    tag = TAGS.get(tag_id, tag_id)
                    exif_data[tag] = str(value)
                metadata["exif"] = exif_data
    except Exception as e:
        logger.warning(f"Failed to extract image metadata from {file_path}: {e}")
    
    return metadata

def unwrap_params(func):
    """Decorator to extract params from custom GPT actions format"""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if request.method == 'POST' or request.method == 'PUT':
            # Handle multipart/form-data with files differently
            if request.files:
                # Check if this is from a custom GPT action
                if 'params.file' in request.files:
                    logger.info("Detected 'params.file' from Custom GPT actions")
                    # Store the files with proper names
                    request.custom_gpt_files = {}
                    for key in request.files:
                        if key.startswith('params.'):
                            new_key = key.replace('params.', '', 1)
                            request.custom_gpt_files[new_key] = request.files[key]
                            
            # Process JSON data if present
            if request.is_json:
                data = request.get_json()
                if isinstance(data, dict) and "params" in data:
                    # Custom GPT actions format: Extract from params
                    logger.info("Detected 'params' wrapper from Custom GPT actions")
                    
                    # Store original data for request context
                    request.original_json = data
                    
                    # Replace request._cached_json to simulate a different request body
                    request._cached_json = (data["params"], request._cached_json[1])
        
        elif request.method == 'GET':
            # For GET requests, params might be in query string
            args_dict = request.args.to_dict()
            if "params.query" in args_dict:
                # Custom GPT actions sometimes flattens params into query strings like params.query
                new_args = {}
                for key, value in args_dict.items():
                    if key.startswith("params."):
                        new_key = key.replace("params.", "", 1)
                        new_args[new_key] = value
                    else:
                        new_args[key] = value
                
                # Store the modified args where endpoints can access them
                request.unwrapped_args = new_args
                logger.info(f"Unwrapped query params: {new_args}")
            
        return func(*args, **kwargs)
    return wrapper

# Apply the unwrap_params decorator to all route functions
app.before_request(lambda: unwrap_params(lambda: None)())

@app.route('/store_file', methods=['POST'])
@api_error_handler
def store_file():
    """Store a file in Firebase Storage with metadata"""
    try:
        # Handle file upload
        if not request.files or 'file' not in request.files and not hasattr(request, 'custom_gpt_files'):
            return jsonify({"status": "error", "error": "No file provided"}), 400
            
        # Get file from either direct upload or custom GPT actions
        file_obj = request.files.get('file')
        if not file_obj and hasattr(request, 'custom_gpt_files'):
            file_obj = request.custom_gpt_files.get('file')
            
        if not file_obj or file_obj.filename == '':
            return jsonify({"status": "error", "error": "No file selected"}), 400
            
        # Get metadata from form or JSON
        metadata = {}
        if request.form:
            metadata = request.form.to_dict()
        elif request.is_json:
            json_data = request.get_json()
            if isinstance(json_data, dict):
                metadata = json_data.get('metadata', {})
                
        # Extract category from metadata or form
        category = metadata.get('category', request.form.get('category', 'default'))
        logger.info(f"Storing file in category: {category}")
        
        # Secure filename and determine file type
        original_filename = secure_filename(file_obj.filename)
        file_ext = os.path.splitext(original_filename)[1].lower()
        
        # Save file to temporary location
        with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as temp_file:
            file_obj.save(temp_file.name)
            temp_path = temp_file.name
        
        try:
            # Detect mime type using python-magic
            mime_type = magic.from_file(temp_path, mime=True)
            
            # Extract text content if possible
            text_content = extract_text_from_file(temp_path, mime_type)
            
            # Extract image metadata if applicable
            additional_metadata = {}
            if mime_type.startswith('image/'):
                additional_metadata = extract_image_metadata(temp_path)
            
            # Generate a unique ID for the file
            file_id = str(uuid.uuid4())
            
            # Prepare storage path
            storage_path = f"{category}/{file_id}{file_ext}"
            
            # Upload to Firebase Storage
            bucket = get_bucket()
            blob = bucket.blob(storage_path)
            
            # Set metadata
            file_metadata = {
                "original_filename": original_filename,
                "content_type": mime_type,
                "category": category,
                "file_id": file_id,
                "created_at": datetime.now().isoformat(),
                "tags": metadata.get('tags', []),
                "has_text_content": bool(text_content),
                "file_size": os.path.getsize(temp_path),
                "description": metadata.get('description', '')
            }
            
            # Add any additional metadata
            file_metadata.update(additional_metadata)
            
            # Convert any non-string values to strings for Firebase
            for key, value in file_metadata.items():
                if not isinstance(value, (str, bool, int, float)):
                    file_metadata[key] = json.dumps(value)
            
            # Upload file with metadata
            blob.metadata = file_metadata
            blob.upload_from_filename(temp_path)
            
            # If we have extracted text content, store it as a separate metadata file
            if text_content:
                text_blob = bucket.blob(f"{category}/{file_id}_text.txt")
                text_blob.upload_from_string(text_content)
                
            # Generate a download URL
            try:
                download_url = blob.generate_signed_url(
                    version="v4",
                    expiration=timedelta(hours=24),
                    method="GET"
                )
            except Exception as url_e:
                logger.warning(f"Could not generate signed URL: {url_e}")
                download_url = None
                
            return jsonify({
                "status": "success",
                "data": {
                    "message": "File stored successfully",
                    "file_id": file_id,
                    "category": category,
                    "storage_path": storage_path,
                    "download_url": download_url,
                    "metadata": file_metadata
                }
            }), 200
            
        finally:
            # Clean up temporary file
            try:
                os.unlink(temp_path)
            except Exception as e:
                logger.warning(f"Error deleting temporary file: {e}")
    
    except Exception as e:
        logger.error(f"Error in store_file: {str(e)}")
        logger.error(f"Exception details: {e.__class__.__name__}: {e}")
        return jsonify({"status": "error", "error": "Internal server error"}), 500

@app.route('/retrieve_file/<file_id>', methods=['GET'])
@api_error_handler
def retrieve_file(file_id):
    """Retrieve a file from Firebase Storage"""
    try:
        # Get query parameters
        args = getattr(request, 'unwrapped_args', request.args)
        category = args.get('category', 'default')
        download = args.get('download', 'false').lower() == 'true'
        
        logger.info(f"Retrieving file: {file_id} from category: {category}")
        
        # Look for file with any extension in the specified category
        bucket = get_bucket()
        blobs = list(bucket.list_blobs(prefix=f"{category}/{file_id}"))
        
        # Filter out text content files and any other metadata files
        file_blobs = [blob for blob in blobs if not blob.name.endswith('_text.txt')]
        
        if not file_blobs:
            return jsonify({"status": "error", "error": "File not found"}), 404
            
        # Use the first matching file
        blob = file_blobs[0]
        
        # Generate download URL if requested
        if not download:
            try:
                download_url = blob.generate_signed_url(
                    version="v4",
                    expiration=timedelta(hours=24),
                    method="GET"
                )
                
                # Get metadata
                metadata = blob.metadata or {}
                
                return jsonify({
                    "status": "success",
                    "data": {
                        "file_id": file_id,
                        "category": category,
                        "download_url": download_url,
                        "metadata": metadata,
                        "name": blob.name,
                        "size": blob.size,
                        "content_type": blob.content_type
                    }
                }), 200
            except Exception as url_e:
                logger.warning(f"Could not generate signed URL: {url_e}")
                # Fall back to direct download
        
        # Download the file to a temporary location
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            blob.download_to_filename(temp_file.name)
            temp_path = temp_file.name
        
        try:
            # Get original filename from metadata or use file_id
            original_filename = None
            if blob.metadata and 'original_filename' in blob.metadata:
                original_filename = blob.metadata['original_filename']
            else:
                # Extract filename from blob name
                original_filename = os.path.basename(blob.name)
                
            # Determine content type
            content_type = blob.content_type
            if not content_type:
                content_type = mimetypes.guess_type(original_filename)[0] or 'application/octet-stream'
                
            # Return file as attachment
            return send_file(
                temp_path,
                mimetype=content_type,
                as_attachment=download,
                download_name=original_filename,
                attachment_filename=original_filename # For older Flask versions
            )
            
        except Exception as send_e:
            logger.error(f"Error sending file: {send_e}")
            os.unlink(temp_path)
            return jsonify({"status": "error", "error": "Error retrieving file"}), 500
            
    except Exception as e:
        logger.error(f"Error in retrieve_file: {str(e)}")
        return jsonify({"status": "error", "error": "Internal server error"}), 500

@app.route('/search_files', methods=['GET'])
@api_error_handler
def search_files():
    """Search for files by metadata, category, or content"""
    try:
        # Get query parameters
        args = getattr(request, 'unwrapped_args', request.args)
        query = args.get('query', '')
        category = args.get('category')
        limit = args.get('limit', default=10, type=int)
        include_text = args.get('include_text', 'false').lower() == 'true'
        
        logger.info(f"Searching files: query='{query}', category='{category}', limit={limit}")
        
        # Get bucket reference
        bucket = get_bucket()
        
        # Build the prefix for listing blobs
        prefix = ""
        if category:
            prefix = f"{category}/"
            
        # List all blobs with the given prefix
        all_blobs = list(bucket.list_blobs(prefix=prefix))
        
        # Filter out text content files initially
        file_blobs = [blob for blob in all_blobs if not blob.name.endswith('_text.txt')]
        text_blobs = {blob.name.replace('_text.txt', ''): blob for blob in all_blobs if blob.name.endswith('_text.txt')}
        
        # Track match quality for ranking
        matches = []
        
        # Search through file metadata
        for blob in file_blobs:
            # Extract file_id from the blob name
            file_id = os.path.splitext(os.path.basename(blob.name))[0]
            
            # Skip if no metadata
            if not blob.metadata:
                continue
                
            metadata = blob.metadata
            match_score = 0
            match_reason = []
            
            # Match by filename
            original_filename = metadata.get('original_filename', '')
            if query.lower() in original_filename.lower():
                match_score += 10
                match_reason.append("filename")
                
            # Match by description
            description = metadata.get('description', '')
            if query.lower() in description.lower():
                match_score += 8
                match_reason.append("description")
                
            # Match by tags
            tags = metadata.get('tags', '')
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except:
                    tags = tags.split(',')
                    
            if isinstance(tags, list) and any(query.lower() in tag.lower() for tag in tags):
                match_score += 15
                match_reason.append("tags")
                
            # Match by category
            if category and query.lower() in category.lower():
                match_score += 5
                match_reason.append("category")
                
            # If we're including text content and there's a text blob
            if include_text and f"{blob.name.rsplit('.', 1)[0]}_text.txt" in [b.name for b in all_blobs]:
                # Download the text content
                text_blob_name = f"{blob.name.rsplit('.', 1)[0]}_text.txt"
                text_blob = bucket.blob(text_blob_name)
                text_content = text_blob.download_as_string().decode('utf-8', errors='ignore')
                
                # Check if query appears in text
                if query.lower() in text_content.lower():
                    match_score += 20
                    match_reason.append("content")
                    
                    # Find context around matched text
                    text_snippet = ""
                    index = text_content.lower().find(query.lower())
                    if index >= 0:
                        start = max(0, index - 100)
                        end = min(len(text_content), index + len(query) + 100)
                        text_snippet = text_content[start:end]
                        
                    metadata['text_snippet'] = text_snippet
                    
            # If we have any matches, add to results
            if match_score > 0:
                # Generate a download URL
                try:
                    download_url = blob.generate_signed_url(
                        version="v4",
                        expiration=timedelta(hours=24),
                        method="GET"
                    )
                except Exception:
                    download_url = None
                    
                matches.append({
                    "file_id": file_id,
                    "name": blob.name,
                    "category": category or blob.name.split('/')[0],
                    "score": match_score,
                    "match_reason": match_reason,
                    "metadata": metadata,
                    "size": blob.size,
                    "content_type": blob.content_type,
                    "download_url": download_url,
                    "created_at": metadata.get('created_at', '')
                })
                
        # Handle empty query case - return recent files
        if not query:
            for blob in file_blobs:
                # Extract file_id from the blob name
                file_id = os.path.splitext(os.path.basename(blob.name))[0]
                metadata = blob.metadata or {}
                
                # Generate a download URL
                try:
                    download_url = blob.generate_signed_url(
                        version="v4",
                        expiration=timedelta(hours=24),
                        method="GET"
                    )
                except Exception:
                    download_url = None
                    
                matches.append({
                    "file_id": file_id,
                    "name": blob.name,
                    "category": category or blob.name.split('/')[0],
                    "score": 1,  # Low score for sorting
                    "match_reason": ["recent"],
                    "metadata": metadata,
                    "size": blob.size,
                    "content_type": blob.content_type,
                    "download_url": download_url,
                    "created_at": metadata.get('created_at', '')
                })
                
        # Sort by score (descending) and then date if available
        matches.sort(key=lambda x: (x["score"], x.get("created_at", "")), reverse=True)
        
        # Apply limit
        if limit > 0:
            matches = matches[:limit]
            
        return jsonify({
            "status": "success",
            "data": {
                "total": len(matches),
                "results": matches
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error in search_files: {str(e)}")
        return jsonify({"status": "error", "error": "Internal server error"}), 500

@app.route('/update_file/<file_id>', methods=['PUT'])
@api_error_handler
def update_file(file_id):
    """Update file metadata or replace file content"""
    try:
        # Get form or JSON data
        metadata = {}
        if request.form:
            metadata = request.form.to_dict()
        elif request.is_json:
            metadata = request.get_json()
            
        if not metadata:
            return jsonify({"status": "error", "error": "No metadata provided"}), 400
            
        # Get category from parameters
        category = metadata.get('category', request.args.get('category', 'default'))
        logger.info(f"Updating file: {file_id} in category: {category}")
        
        # Find the file in Firebase Storage
        bucket = get_bucket()
        blobs = list(bucket.list_blobs(prefix=f"{category}/{file_id}"))
        
        # Filter out text content files
        file_blobs = [blob for blob in blobs if not blob.name.endswith('_text.txt')]
        
        if not file_blobs:
            return jsonify({"status": "error", "error": "File not found"}), 404
            
        # Use the first matching file
        blob = file_blobs[0]
        
        # Check if we're uploading a new file
        file_updated = False
        if request.files and 'file' in request.files:
            file_obj = request.files['file']
            if file_obj and file_obj.filename != '':
                with tempfile.NamedTemporaryFile(delete=False) as temp_file:
                    file_obj.save(temp_file.name)
                    temp_path = temp_file.name
                    
                try:
                    # Detect MIME type
                    mime_type = magic.from_file(temp_path, mime=True)
                    
                    # Extract text content if possible
                    text_content = extract_text_from_file(temp_path, mime_type)
                    
                    # Extract image metadata if applicable
                    additional_metadata = {}
                    if mime_type.startswith('image/'):
                        additional_metadata = extract_image_metadata(temp_path)
                        
                    # Update metadata with file info
                    metadata.update({
                        "content_type": mime_type,
                        "file_size": os.path.getsize(temp_path),
                        "updated_at": datetime.now().isoformat(),
                        "has_text_content": bool(text_content)
                    })
                    
                    # Add additional metadata
                    metadata.update(additional_metadata)
                    
                    # Upload new file
                    blob.upload_from_filename(temp_path)
                    file_updated = True
                    
                    # If we have extracted text content, update it
                    if text_content:
                        text_blob_name = f"{category}/{file_id}_text.txt"
                        text_blob = bucket.blob(text_blob_name)
                        text_blob.upload_from_string(text_content)
                        
                finally:
                    # Clean up
                    try:
                        os.unlink(temp_path)
                    except:
                        pass
        
        # Update metadata
        current_metadata = blob.metadata or {}
        current_metadata.update(metadata)
        
        # Convert any non-string values to strings for Firebase
        for key, value in current_metadata.items():
            if not isinstance(value, (str, bool, int, float)):
                current_metadata[key] = json.dumps(value)
                
        # Update the blob metadata
        blob.metadata = current_metadata
        blob.patch()
        
        # Generate a download URL
        try:
            download_url = blob.generate_signed_url(
                version="v4",
                expiration=timedelta(hours=24),
                method="GET"
            )
        except Exception as url_e:
            logger.warning(f"Could not generate signed URL: {url_e}")
            download_url = None
            
        return jsonify({
            "status": "success",
            "data": {
                "message": "File updated successfully",
                "file_id": file_id,
                "category": category,
                "storage_path": blob.name,
                "download_url": download_url,
                "metadata": current_metadata,
                "file_updated": file_updated
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error in update_file: {str(e)}")
        return jsonify({"status": "error", "error": "Internal server error"}), 500

@app.route('/delete_file/<file_id>', methods=['DELETE'])
@api_error_handler
def delete_file(file_id):
    """Delete a file from Firebase Storage"""
    try:
        # Get category from parameters
        category = request.args.get('category', 'default')
        logger.info(f"Deleting file: {file_id} from category: {category}")
        
        # Find the file in Firebase Storage
        bucket = get_bucket()
        blobs = list(bucket.list_blobs(prefix=f"{category}/{file_id}"))
        
        if not blobs:
            return jsonify({"status": "error", "error": "File not found"}), 404
            
        # Delete all matching blobs (including text content)
        for blob in blobs:
            blob.delete()
            logger.info(f"Deleted blob: {blob.name}")
            
        return jsonify({
            "status": "success",
            "data": {
                "message": "File deleted successfully",
                "file_id": file_id,
                "category": category
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error in delete_file: {str(e)}")
        return jsonify({"status": "error", "error": "Internal server error"}), 500

@app.route('/recent_files', methods=['GET'])
@api_error_handler
def get_recent_files():
    """Get recently uploaded files, optionally filtered by category"""
    try:
        # Get query parameters
        args = getattr(request, 'unwrapped_args', request.args)
        category = args.get('category')
        limit = args.get('limit', default=10, type=int)
        
        logger.info(f"Getting recent files: category='{category}', limit={limit}")
        
        # Get bucket reference
        bucket = get_bucket()
        
        # Build the prefix for listing blobs
        prefix = ""
        if category:
            prefix = f"{category}/"
            
        # List all blobs with the given prefix
        all_blobs = list(bucket.list_blobs(prefix=prefix))
        
        # Filter out text content files
        file_blobs = [blob for blob in all_blobs if not blob.name.endswith('_text.txt')]
        
        # Create result objects with metadata
        results = []
        for blob in file_blobs:
            # Extract file_id from the blob name
            file_parts = blob.name.split('/')
            if len(file_parts) < 2:
                continue
                
            file_category = file_parts[0]
            file_id = os.path.splitext(file_parts[-1])[0]
            metadata = blob.metadata or {}
            
            # Generate a download URL
            try:
                download_url = blob.generate_signed_url(
                    version="v4",
                    expiration=timedelta(hours=24),
                    method="GET"
                )
            except Exception:
                download_url = None
                
            # Create result object
            file_obj = {
                "file_id": file_id,
                "name": blob.name,
                "category": file_category,
                "size": blob.size,
                "content_type": blob.content_type or "application/octet-stream",
                "download_url": download_url,
                "metadata": metadata
            }
            
            # Add created_at if available
            if 'created_at' in metadata:
                file_obj['created_at'] = metadata['created_at']
                
            results.append(file_obj)
            
        # Sort by created_at if available, otherwise by name
        results.sort(key=lambda x: (x.get('created_at', '') or x['name']), reverse=True)
        
        # Apply limit
        if limit > 0:
            results = results[:limit]
            
        return jsonify({
            "status": "success",
            "data": {
                "total": len(results),
                "results": results
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error in get_recent_files: {str(e)}")
        return jsonify({"status": "error", "error": "Internal server error"}), 500

@app.route('/file_categories', methods=['GET'])
@api_error_handler
def get_file_categories():
    """Get a list of all file categories in storage"""
    try:
        logger.info("Getting file categories")
        
        # Get bucket reference
        bucket = get_bucket()
        
        # List all blobs
        all_blobs = list(bucket.list_blobs())
        
        # Extract unique categories
        categories = set()
        for blob in all_blobs:
            parts = blob.name.split('/')
            if len(parts) > 1:
                categories.add(parts[0])
                
        return jsonify({
            "status": "success",
            "data": {
                "categories": sorted(list(categories))
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error in get_file_categories: {str(e)}")
        return jsonify({"status": "error", "error": "Internal server error"}), 500

@app.route('/file_info/<file_id>', methods=['GET'])
@api_error_handler
def get_file_info(file_id):
    """Get detailed information about a specific file"""
    try:
        # Get query parameters
        args = getattr(request, 'unwrapped_args', request.args)
        category = args.get('category', 'default')
        include_text = args.get('include_text', 'false').lower() == 'true'
        
        logger.info(f"Getting file info: {file_id} from category: {category}")
        
        # Find the file in Firebase Storage
        bucket = get_bucket()
        blobs = list(bucket.list_blobs(prefix=f"{category}/{file_id}"))
        
        # Filter out text content files
        file_blobs = [blob for blob in blobs if not blob.name.endswith('_text.txt')]
        
        if not file_blobs:
            return jsonify({"status": "error", "error": "File not found"}), 404
            
        # Use the first matching file
        blob = file_blobs[0]
        metadata = blob.metadata or {}
        
        # Generate a download URL
        try:
            download_url = blob.generate_signed_url(
                version="v4",
                expiration=timedelta(hours=24),
                method="GET"
            )
        except Exception:
            download_url = None
            
        # Create result object
        result = {
            "file_id": file_id,
            "name": blob.name,
            "category": category,
            "size": blob.size,
            "content_type": blob.content_type or "application/octet-stream",
            "download_url": download_url,
            "metadata": metadata
        }
        
        # Include text content if requested
        if include_text:
            text_blob_name = f"{category}/{file_id}_text.txt"
            text_blob = bucket.blob(text_blob_name)
            if text_blob.exists():
                result["text_content"] = text_blob.download_as_string().decode('utf-8', errors='ignore')
                
        return jsonify({
            "status": "success",
            "data": result
        }), 200
        
    except Exception as e:
        logger.error(f"Error in get_file_info: {str(e)}")
        return jsonify({"status": "error", "error": "Internal server error"}), 500

@app.route('/edit_document/<file_id>', methods=['POST'])
@api_error_handler
def edit_document(file_id):
    """Edit a document and save the results"""
    try:
        # Import the document editor module
        from document_editor import get_editor_for_file
        
        # Get the edit instructions from the request
        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "error": "No edit instructions provided"}), 400
            
        edit_instructions = data.get('edit_instructions', {})
        category = data.get('category', 'default')
        
        logger.info(f"Editing document: {file_id} in category: {category}")
        
        # Find the file in Firebase Storage
        bucket = get_bucket()
        blobs = list(bucket.list_blobs(prefix=f"{category}/{file_id}"))
        
        # Filter out text content files
        file_blobs = [blob for blob in blobs if not blob.name.endswith('_text.txt')]
        
        if not file_blobs:
            return jsonify({"status": "error", "error": "File not found"}), 404
            
        # Use the first matching file
        blob = file_blobs[0]
        original_filename = blob.metadata.get('original_filename') if blob.metadata else f"{file_id}{os.path.splitext(blob.name)[1]}"
        
        # Download the file to a temporary location
        with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(blob.name)[1]) as temp_file:
            blob.download_to_filename(temp_file.name)
            temp_path = temp_file.name
            
        try:
            # Get the appropriate editor for this file type
            editor = get_editor_for_file(temp_path)
            if not editor:
                return jsonify({"status": "error", "error": "Unsupported file type for editing"}), 400
                
            # Load the document
            if not editor.load(temp_path):
                return jsonify({"status": "error", "error": "Failed to load document"}), 500
                
            # Get original content
            original_content = editor.get_content()
            
            # Apply edits
            if not editor.edit_content(edit_instructions):
                return jsonify({"status": "error", "error": "Failed to apply edits"}), 400
                
            # Get edited content
            edited_content = editor.get_content()
            
            # Create a new temp file for the edited content
            with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(blob.name)[1]) as edited_file:
                edited_path = edited_file.name
                
            # Save the edited document
            if not editor.save(edited_path):
                os.unlink(edited_path)
                return jsonify({"status": "error", "error": "Failed to save edited document"}), 500
                
            # Prepare storage path for edited version
            storage_path = f"{category}/{file_id}_edited{os.path.splitext(blob.name)[1]}"
            edited_blob = bucket.blob(storage_path)
            
            # Update metadata
            metadata = blob.metadata.copy() if blob.metadata else {}
            metadata.update({
                "edited_at": datetime.now().isoformat(),
                "original_file_id": file_id,
                "edit_operation": edit_instructions.get('operation', 'unknown')
            })
            
            # Upload edited file
            edited_blob.metadata = metadata
            edited_blob.upload_from_filename(edited_path)
            
            # Extract text content if possible
            mime_type = magic.from_file(edited_path, mime=True)
            text_content = extract_text_from_file(edited_path, mime_type)
            
            if text_content:
                text_blob = bucket.blob(f"{category}/{file_id}_edited_text.txt")
                text_blob.upload_from_string(text_content)
            
            # Generate download URL
            try:
                download_url = edited_blob.generate_signed_url(
                    version="v4",
                    expiration=timedelta(hours=24),
                    method="GET"
                )
            except Exception:
                download_url = None
                
            # Prepare result
            result = {
                "file_id": f"{file_id}_edited",
                "original_file_id": file_id,
                "name": edited_blob.name,
                "category": category,
                "size": edited_blob.size,
                "content_type": edited_blob.content_type,
                "download_url": download_url,
                "metadata": metadata,
                "edit_operation": edit_instructions.get('operation', 'unknown'),
                "success": True
            }
            
            # Clean up temp files
            try:
                os.unlink(temp_path)
                os.unlink(edited_path)
            except:
                pass
                
            return jsonify({
                "status": "success",
                "data": result
            }), 200
            
        except Exception as edit_e:
            logger.error(f"Error editing document: {edit_e}")
            # Clean up temp files
            try:
                os.unlink(temp_path)
            except:
                pass
            return jsonify({"status": "error", "error": f"Error editing document: {str(edit_e)}"}), 500
            
    except Exception as e:
        logger.error(f"Error in edit_document: {str(e)}")
        return jsonify({"status": "error", "error": "Internal server error"}), 500

@app.route('/health_check', methods=['GET'])
def health_check():
    """Simple health check endpoint"""
    return jsonify({
        "status": "success",
        "data": {
            "message": "API is running",
            "storage": "Firebase Storage"
        }
    }), 200

@app.route('/system_stats', methods=['GET'])
@api_error_handler
def get_system_stats():
    """Get system statistics"""
    try:
        logger.info("Getting system statistics")
        
        # Get bucket reference and count files
        bucket = get_bucket()
        all_blobs = list(bucket.list_blobs())
        
        # Count categories, files, and total storage
        categories = set()
        file_count = 0
        total_size = 0
        
        for blob in all_blobs:
            parts = blob.name.split('/')
            if len(parts) > 1:
                categories.add(parts[0])
                
            if not blob.name.endswith('_text.txt'):
                file_count += 1
                total_size += blob.size
                
        stats = {
            "categories": len(categories),
            "file_count": file_count,
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "cache_info": {
                "bucket_cache_size": len(bucket_cache),
                "results_cache_size": len(results_cache),
                "metadata_cache_size": len(metadata_cache)
            }
        }
        
        return jsonify({
            "status": "success",
            "data": stats
        }), 200
        
    except Exception as e:
        logger.error(f"Error getting system stats: {e}")
        return jsonify({
            "status": "error",
            "error": "Could not retrieve system statistics"
        }), 500

if __name__ == '__main__':
    # Get port from environment variable for Cloud Run
    port = int(os.environ.get('PORT', 8080))
    
    # Add readiness check
    @app.route('/_ah/warmup')
    def warmup():
        return '', 200
        
    # Run the app with proper host binding for containers
    app.run(host='0.0.0.0', port=port, debug=False)