import json, os
from datetime import datetime, timezone
from pathlib import Path
import requests

ROOT=Path(__file__).parent
CONFIG=json.loads((ROOT/"config/routes.json").read_text())
DATA=ROOT/"docs/data/prices.json"
BASE=os.getenv("AMADEUS_BASE_URL","https://test.api.amadeus.com")
def telegram(message):
    token,chat=os.getenv("TELEGRAM_BOT_TOKEN"),os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        r=requests.post(f"https://api.telegram.org/bot{token}/sendMessage",json={"chat_id":chat,"text":message},timeout=30); r.raise_for_status()
def main():
    cid,secret=os.getenv("AMADEUS_CLIENT_ID"),os.getenv("AMADEUS_CLIENT_SECRET")
    if not cid or not secret: raise SystemExit("Missing AMADEUS_CLIENT_ID / AMADEUS_CLIENT_SECRET secrets.")
    r=requests.post(f"{BASE}/v1/security/oauth2/token",data={"grant_type":"client_credentials","client_id":cid,"client_secret":secret},timeout=30); r.raise_for_status()
    headers={"Authorization":"Bearer "+r.json()["access_token"]}
    state=json.loads(DATA.read_text()) if DATA.exists() else {"history":[],"results":[]}
    now=datetime.now(timezone.utc).isoformat(); results=[]
    for route in CONFIG["routes"]:
        params={"originLocationCode":CONFIG.get("origin","KUL"),"destinationLocationCode":route["destination"],"departureDate":CONFIG["departure_date"],"returnDate":CONFIG["return_date"],"adults":CONFIG.get("adults",1),"travelClass":CONFIG.get("travel_class","ECONOMY"),"currencyCode":CONFIG.get("currency","MYR"),"max":10}
        try:
            r=requests.get(f"{BASE}/v2/shopping/flight-offers",headers=headers,params=params,timeout=45); r.raise_for_status()
            offers=r.json().get("data",[])
            if not offers: print("No offers:",route["destination"]); continue
            offer=min(offers,key=lambda x:float(x["price"]["grandTotal"])); price=round(float(offer["price"]["grandTotal"]),2)
            item={"origin":CONFIG.get("origin","KUL"),"destination":route["destination"],"departure_date":CONFIG["departure_date"],"return_date":CONFIG["return_date"],"price":price,"currency":CONFIG.get("currency","MYR"),"budget":route["budget_myr"],"below_budget":price<=route["budget_myr"],"checked_at":now}
            results.append(item); state.setdefault("history",[]).append(item)
            # Alert only on first qualifying observation or when price improves, avoiding daily repeated alerts.
            previous=[x for x in state["history"][:-1] if x.get("destination")==route["destination"]]
            if item["below_budget"] and (not previous or price < min(x["price"] for x in previous)):
                telegram(f"✈️ Her Wanderlust Path\nKUL → {route['destination']}\n{item['departure_date']} 至 {item['return_date']}\n报价 RM{price:.2f}（预算 RM{route['budget_myr']:.2f}）\n请再次确认实时价格、行李及费用。")
            print(route["destination"],price)
        except requests.RequestException as e: print("Request failed",route["destination"],str(e))
    state.update({"updated_at":now,"currency":CONFIG.get("currency","MYR"),"results":results})
    DATA.write_text(json.dumps(state,ensure_ascii=False,indent=2)+"\n")
if __name__=="__main__": main()
