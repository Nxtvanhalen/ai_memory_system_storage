# AI Memory Storage System

A robust Firebase Storage API for storing and retrieving documents, images, and other file types for Custom GPTs. This system complements the existing Firebase Realtime Database memory system by providing file storage capabilities.

## Features

- Store any file type in Firebase Storage with metadata
- Retrieve files by ID with download links or direct download
- Search files by content, metadata, and category
- Automatic text extraction from supported file types (PDF, DOCX, TXT)
- Image metadata extraction for image files
- File categorization and tagging
- Recent files listing and category browsing
- Document editing for supported file types (Word, Excel, Pages, Numbers, text)
- Compatible with OpenAI's Custom GPT actions

## Supported File Types

The system supports storing any file type, with special handling for:

- **Text files** (.txt, .md, .json, etc.) - Text extraction, full text editing
- **PDFs** - Text extraction
- **Office documents** (.docx, .xlsx) - Text extraction, content editing
  - Word: text replacement, paragraph addition, find & replace
  - Excel: cell updates, sheet management, data range operations
- **Apple iWork formats** (.pages, .numbers, .keynote) - Text extraction, basic text editing
  - Pages: text export and editing
  - Numbers: data export to CSV format
- **Images** (.jpg, .png, .gif, etc.) - Metadata extraction
- **Any other file type** - Basic storage and retrieval

## API Endpoints

### Storage Operations

- **POST /store_file** - Upload a file with metadata
- **GET /retrieve_file/{file_id}** - Get download link or download file
- **PUT /update_file/{file_id}** - Update file metadata or content
- **DELETE /delete_file/{file_id}** - Delete a file
- **POST /edit_document/{file_id}** - Edit document content with specific operations

### Search and Discovery

- **GET /search_files** - Search files by query and metadata
- **GET /recent_files** - List recently uploaded files
- **GET /file_categories** - List all file categories
- **GET /file_info/{file_id}** - Get detailed file information

### System

- **GET /health_check** - Check if API is running
- **GET /system_stats** - Get system statistics

## Setup and Deployment

### Prerequisites

- Firebase project with Storage enabled
- Firebase service account credentials
- Python 3.11 or higher

### Environment Variables

- `FIREBASE_STORAGE_BUCKET` - Firebase Storage bucket name
- `PROJECT_ID` - Firebase project ID
- `FIREBASE_SERVICE_ACCOUNT` - (Optional) Service account JSON as environment variable

### Local Development

1. Clone the repository
2. Install dependencies: `pip install -r requirements.txt`
3. Place the Firebase service account JSON file in the root directory
4. Run the application: `python app.py`

### Deployment

The app is configured for deployment on Render.com using Docker. 

1. Set up a new Web Service on Render.com
2. Connect to your Git repository
3. Use the Docker deployment option
4. Set the required environment variables
5. Deploy

## Custom GPT Integration

To use this API with a Custom GPT:

1. Add the OpenAPI schema (`openapi_schema.json`) to your Custom GPT's actions
2. Use the actions to upload, retrieve, and search files
3. Include file handling in your GPT's instructions

## Implementation Details

- Uses Firebase Admin SDK for Storage operations
- Implements caching for performance optimization
- Thread pool for parallel operations
- Robust error handling and logging
- File content extraction for improved searchability
- Metadata enrichment for better organization