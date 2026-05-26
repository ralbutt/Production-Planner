import os
from datetime import timedelta
from flask import Flask, redirect, url_for
from database import init_db
from blueprints.auth      import auth_bp
from blueprints.manager   import manager_bp
from blueprints.operator  import operator_bp
from blueprints.employees import employees_bp
from blueprints.users     import users_bp
from blueprints.library   import library_bp
from blueprints.jobs      import jobs_bp
from blueprints.scheduler import scheduler_bp
from blueprints.availability import availability_bp
from blueprints.sales_orders import sales_orders_bp
from blueprints.imports import imports_bp
from blueprints.display import display_bp

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")
app.permanent_session_lifetime = timedelta(days=7)

for bp in [auth_bp, manager_bp, operator_bp, employees_bp,
           users_bp, library_bp, jobs_bp, scheduler_bp, availability_bp, sales_orders_bp, imports_bp, display_bp]:
    app.register_blueprint(bp)

@app.route("/")
def index():
    return redirect(url_for("auth.login"))

with app.app_context():
    init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5000)


if __name__ == '__main__':
    app.run(debug=True)
