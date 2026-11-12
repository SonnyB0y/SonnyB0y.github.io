"""Flask: website med søjlediagram af spritniveau + JSON-API med API-nøgle.
Kør: API_KEY=hemmelig python3 app.py   (se README for miljøvariabler til databasen)"""
import hmac
import json
import os
import sys

import plotly.graph_objects as go
from flask import Flask, abort, jsonify, render_template, request
from plotly.utils import PlotlyJSONEncoder

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import db  # noqa: E402

app = Flask(__name__)
API_KEY = os.getenv("API_KEY", "")
BG = "#272b34"


def build_figure(rows):
    """Søjle pr. beholder; rød hvis niveauet er under grænsen, ellers grøn."""
    labels = [f'{r["hospital"]}, {r["lokale"]}' for r in rows]
    values = [r["indhold"] for r in rows]
    colors = ["#e5484d" if r["lav"] else "#30a46c" for r in rows]
    fig = go.Figure(go.Bar(x=labels, y=values, marker_color=colors,
                           text=[f"{v} %" for v in values], textposition="outside",
                           hovertemplate="%{x}<br>%{y} %<extra></extra>"))
    fig.add_hline(y=db.LAV_GRAENSE, line_dash="dash", line_color="#f5a524",
                  annotation_text=f"Lav ({db.LAV_GRAENSE} %)", annotation_position="top left")
    fig.update_layout(template="plotly_dark", paper_bgcolor=BG, plot_bgcolor=BG,
                      yaxis=dict(title="Fyldning (%)", range=[0, 110]),
                      xaxis=dict(title=None), margin=dict(t=30))
    return fig


@app.route("/")
def index():
    rows = db.latest_levels()
    graph_json = json.dumps(build_figure(rows), cls=PlotlyJSONEncoder)
    return render_template("bar.html", graphJSON=graph_json, rows=rows)


def require_key():
    given = request.headers.get("X-API-Key", "")
    if not API_KEY or not hmac.compare_digest(given, API_KEY):
        abort(401)


@app.route("/api/status")
def api_status():
    require_key()
    return jsonify(db.latest_levels())


@app.route("/api/historik/<int:beholder_id>")
def api_historik(beholder_id):
    require_key()
    return jsonify(db.level_history(beholder_id))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))