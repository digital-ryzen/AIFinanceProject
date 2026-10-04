import os
from fastapi import FastAPI
from fastapi.responses import FileResponse
import yfinance as yf
from google import genai
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore

# 1. Load secret variables from .env
load_dotenv()

app = FastAPI()

# 2. Initialize Gemini API Client
secure_api_key = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=secure_api_key)

# 3. Initialize Firebase Firestore
cred = credentials.Certificate("firebase-key.json")
firebase_admin.initialize_app(cred)
db = firestore.client()


# Helper function to format large revenue figures
def format_currency(value):
    if value is None:
        return "N/A"
    try:
        val = float(value)
        if val >= 1e9:
            return f"${val / 1e9:.2f}B"
        if val >= 1e6:
            return f"${val / 1e6:.2f}M"
        return f"${val:,.2f}"
    except Exception:
        return "N/A"


@app.get("/")
def serve_home_page():
    return FileResponse("index.html")


@app.get("/analyze/{ticker_symbol}")
def analyze_and_save_stock(ticker_symbol: str, session_id: str):
    ticker_symbol = ticker_symbol.upper()
    stock = yf.Ticker(ticker_symbol)
    company_info = stock.info

    # Fetch Base Metrics (With fallback to fast_info)
    current_price = company_info.get("currentPrice")
    if current_price is None:
        try:
            current_price = stock.fast_info.last_price
        except Exception:
            current_price = None

    profit_margin = company_info.get("profitMargins")
    formatted_price = f"${current_price:.2f}" if current_price else "N/A"
    formatted_margin = f"{profit_margin:.2%}" if profit_margin else "N/A"

    # Fetch Yearly Metrics (With fallback to income statement)
    y_rev_raw = company_info.get("totalRevenue")
    y_eps_raw = company_info.get("trailingEps")

    try:
        if y_rev_raw is None or y_eps_raw is None:
            income_stmt = stock.income_stmt
            if not income_stmt.empty:
                if y_rev_raw is None and "Total Revenue" in income_stmt.index:
                    y_rev_raw = income_stmt.loc["Total Revenue"].iloc[0]
                if y_eps_raw is None and "Basic EPS" in income_stmt.index:
                    y_eps_raw = income_stmt.loc["Basic EPS"].iloc[0]
    except Exception:
        pass

    y_rev = format_currency(y_rev_raw)

    if y_eps_raw is not None and str(y_eps_raw).lower() != "nan":
        y_eps = f"${float(y_eps_raw):.2f}"
    else:
        y_eps = "N/A"

    # Fetch Quarterly Metrics (With NaN interception)
    q_rev = "N/A"
    q_eps = "N/A"
    try:
        q_income = stock.quarterly_income_stmt
        if not q_income.empty:
            if "Total Revenue" in q_income.index:
                rev_val = q_income.loc["Total Revenue"].iloc[0]
                if str(rev_val).lower() != "nan":
                    q_rev = format_currency(rev_val)

            if "Basic EPS" in q_income.index:
                eps_val = q_income.loc['Basic EPS'].iloc[0]
                if str(eps_val).lower() != "nan":
                    q_eps = f"${float(eps_val):.2f}"
            elif "Diluted EPS" in q_income.index:
                eps_val = q_income.loc['Diluted EPS'].iloc[0]
                if str(eps_val).lower() != "nan":
                    q_eps = f"${float(eps_val):.2f}"
    except Exception:
        pass

    # Prompt construction for Gemini
    prompt = (
        f"{ticker_symbol}'s current price is {formatted_price}, profit margin is {formatted_margin}. "
        f"Yearly Revenue: {y_rev}, Quarterly Revenue: {q_rev}. "
        f"Yearly EPS: {y_eps}, Quarterly EPS: {q_eps}. "
        f"As a financial analyst, is this a healthy financial profile for a company in its sector? "
        f"Keep your answer to two sentences."
    )

    try:
        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt,
        )
        ai_text = response.text

        # Save document to an isolated user session folder in Firestore
        new_record = {
            "ticker": ticker_symbol,
            "price": formatted_price,
            "profit_margin": formatted_margin,
            "q_rev": q_rev,
            "y_rev": y_rev,
            "q_eps": q_eps,
            "y_eps": y_eps,
            "analysis": ai_text,
            "timestamp": firestore.SERVER_TIMESTAMP,
        }

        db.collection("sessions").document(session_id).collection("ai_analyses").add(new_record)

        return {
            "status": "Success! Saved to Firebase.",
            "ticker": ticker_symbol,
            "price": formatted_price,
            "profit_margin": formatted_margin,
            "q_rev": q_rev,
            "y_rev": y_rev,
            "q_eps": q_eps,
            "y_eps": y_eps,
            "ai_response": ai_text,
        }

    except Exception as e:
        return {
            "status": "Failed to generate AI analysis.",
            "error_details": str(e),
        }


@app.get("/database")
def view_database(session_id: str):
    # Retrieve documents only from this user's specific session folder
    docs = (
        db.collection("sessions").document(session_id).collection("ai_analyses")
        .order_by("timestamp", direction=firestore.Query.ASCENDING)
        .stream()
    )

    formatted_rows = []
    for doc in docs:
        data = doc.to_dict()
        formatted_rows.append([
            doc.id,
            data.get("ticker", "N/A"),
            data.get("price", "N/A"),
            data.get("profit_margin", "N/A"),
            data.get("q_rev", "N/A"),
            data.get("y_rev", "N/A"),
            data.get("q_eps", "N/A"),
            data.get("y_eps", "N/A"),
            data.get("analysis", "N/A"),
        ])

    return {"saved_records": formatted_rows}