# ATI Manual Finder

A local web app for ATI of America's project management team. Upload an AV project contract PDF, automatically find product manuals and warranty info, and export a formatted Excel handoff package.

## One-Time Setup

### 1. Install Python 3.10+
```bash
brew install python@3.12
```

### 2. Install dependencies
```bash
cd ati-manual-finder
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Set up Supabase database
- Open your Supabase project dashboard
- Go to **SQL Editor**
- Paste the contents of `supabase_schema.sql` and run it

### 4. Create storage bucket
- In Supabase, go to **Storage** → **New Bucket**
- Name it `manuals`
- Leave it as **private**

### 5. Configure environment variables
```bash
cp .env.example .env
```
Edit `.env` and fill in:
- `SUPABASE_URL` — your Supabase project URL (e.g. `https://abc123.supabase.co`)
- `SUPABASE_KEY` — your Supabase anon or service role key
- `ANTHROPIC_API_KEY` — your Anthropic API key

## Running the App

```bash
cd ati-manual-finder
source .venv/bin/activate
cd app
python -m uvicorn main:app --reload --port 8000
```

Open [http://localhost:8000](http://localhost:8000)

## Day-to-Day Use

1. Click **+ New Project** → enter the job name → upload the contract PDF → click **Upload & Process**
2. Wait for results (the progress bar shows live status)
3. Paste in manual URLs for anything flagged "Not Found"
4. Click **Export Excel** for the client handoff package

## Re-Running for Change Orders

Delete the project and re-upload the updated PDF, or use the **Retry** button on individual items.
