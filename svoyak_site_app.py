import sqlite3
from flask import Flask, render_template, request
import datetime
import time

app = Flask(__name__)


def get_db_connection():
    conn = sqlite3.connect('./svoyak.db')
    conn.row_factory = sqlite3.Row
    return conn


@app.route('/')
def ratingindex():
    return "secret svoyak page"

@app.route('/about')
def about():
    return "About page"
    
def read_cfg():
    website_url = ""
    try:
        with open("site.cfg") as cfg_file:
            website_url = cfg_file.readline().strip()
    except Exception:
        pass    
    if website_url == "":
        website_url = 'svoyak.chgk.fun'
    return website_url

if __name__ == "__main__":
    website_url = read_cfg()
    print(website_url)
    app.config['SERVER_NAME'] = website_url
    app.run(debug=True, port=80)