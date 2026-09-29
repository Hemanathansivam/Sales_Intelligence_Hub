import streamlit as st
import sqlite3
import pandas as pd
import datetime

st.set_page_config(page_title="Sales Intelligence Hub", page_icon="📊", layout="wide")


import os

# Resolve the DB path relative to THIS SCRIPT'S OWN FOLDER -- not wherever
# `streamlit run` happens to be launched from. This is what fixes
# "execution successful but nothing changes": a relative path silently
# connects to (or creates) a DIFFERENT file if you launch Streamlit from
# a different working directory than your notebook used.
DB_FILENAME = "Sales_Intelligence_Hub_Database.db"
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), DB_FILENAME)


@st.cache_resource
def get_connection():
    connection = sqlite3.connect(DB_PATH, check_same_thread=False)
    connection.execute("PRAGMA foreign_keys = ON;")
    return connection


connection = get_connection()

# Sanity check: confirm this is really pointing at your POPULATED database,
# not a blank file SQLite silently created because the path was wrong.
_existing_tables = set(pd.read_sql(
    "SELECT name FROM sqlite_master WHERE type='table';", connection
)["name"].tolist())
_required_tables = {"branches", "customer_sales_table", "users", "payment_splits"}

if not _required_tables.issubset(_existing_tables):
    st.error(
        f"Connected to: {DB_PATH}\n\n"
        f"But expected tables are missing (found: {sorted(_existing_tables)}). "
        f"This means Streamlit is pointing at a different .db file than your notebook. "
        f"Move streamlit_app.py into the EXACT SAME FOLDER as your .db file and restart."
    )
    st.stop()


# ===========================================================================
# BACKEND FUNCTIONS -- authenticate() and get_sales() match your notebook exactly
# ===========================================================================

def authenticate(username, password, connection):
    query = "SELECT user_id, username, role, branch_id FROM users WHERE username = ? AND password = ?;"
    result = pd.read_sql(query, connection, params=(username, password))
    if result.empty:
        return None  # login failed
    return result.iloc[0]  # a row with user_id, username, role, branch_id


def get_sales(connection, role, branch_id, status_filter=None):
    if role == "Super Admin":
        query = "SELECT cs.*, b.branch_name FROM customer_sales_table cs LEFT JOIN branches b ON cs.branch_id = b.branch_id"
        params = []
    else:  # Admin
        query = "SELECT cs.*, b.branch_name FROM customer_sales_table cs LEFT JOIN branches b ON cs.branch_id = b.branch_id WHERE cs.branch_id = ?"
        params = [branch_id]

    if status_filter and status_filter != "All":
        query += (" AND" if params else " WHERE") + " cs.status = ?"
        params.append(status_filter)

    query += " ORDER BY cs.sale_id DESC;"
    return pd.read_sql(query, connection, params=params)


def get_branches(connection):
    return pd.read_sql("SELECT branch_id, branch_name, branch_admin_name FROM branches;", connection)


def get_branch_name(connection, branch_id):
    row = pd.read_sql("SELECT branch_name FROM branches WHERE branch_id = ?;", connection, params=(branch_id,))
    return row.iloc[0]["branch_name"] if not row.empty else "Unknown"


def sale_belongs_to_branch(connection, sale_id, branch_id):
    row = pd.read_sql("SELECT branch_id FROM customer_sales_table WHERE sale_id = ?;", connection, params=(sale_id,))
    if row.empty:
        return False
    return int(row.iloc[0]["branch_id"]) == int(branch_id)


def insert_sale(connection, branch_id, date, name, mobile_number, product_name, gross_sales):
    # pending_amount is filled automatically by trg_init_pending -- never set it manually here
    cursor = connection.cursor()
    try:
        cursor.execute(
            """INSERT INTO customer_sales_table
               (branch_id, date, name, mobile_number, product_name, gross_sales)
               VALUES (?, ?, ?, ?, ?, ?);""",
            (branch_id, date, name, mobile_number, product_name, gross_sales)
        )
        connection.commit()
        return True, "Sale added successfully.", cursor.lastrowid
    except sqlite3.IntegrityError as e:
        return False, f"Could not add sale: {e}", None


def insert_payment(connection, sale_id, payment_date, amount_paid, payment_method):
    # trg_pending_amt + trg_close_sale update received_amount / pending_amount / status automatically.
    # trg_reject_closed raises IntegrityError if the sale is already Closed -- caught below.
    cursor = connection.cursor()
    try:
        cursor.execute(
            """INSERT INTO payment_splits (sale_id, payment_date, amount_paid, payment_method)
               VALUES (?, ?, ?, ?);""",
            (sale_id, payment_date, amount_paid, payment_method)
        )
        connection.commit()
        return True, "Payment recorded successfully."
    except sqlite3.IntegrityError as e:
        return False, f"Payment rejected: {e}"


def get_sale_row(connection, sale_id):
    return pd.read_sql("SELECT * FROM customer_sales_table WHERE sale_id = ?;", connection, params=(sale_id,))


def get_payment_history(connection, sale_id):
    return pd.read_sql("SELECT * FROM payment_splits WHERE sale_id = ? ORDER BY payment_date;", connection, params=(sale_id,))


def get_kpis(connection, role, branch_id):
    query = ("SELECT COALESCE(SUM(gross_sales),0) AS total_gross, "
             "COALESCE(SUM(received_amount),0) AS total_received, "
             "COALESCE(SUM(pending_amount),0) AS total_pending FROM customer_sales_table")
    params = []
    if role != "Super Admin":
        query += " WHERE branch_id = ?"
        params.append(branch_id)
    return pd.read_sql(query, connection, params=params).iloc[0]


def get_payment_method_summary(connection, role, branch_id):
    query = """
        SELECT ps.payment_method, COALESCE(SUM(ps.amount_paid),0) AS total_collected
        FROM payment_splits ps JOIN customer_sales_table cs ON ps.sale_id = cs.sale_id
    """
    params = []
    if role != "Super Admin":
        query += " WHERE cs.branch_id = ?"
        params.append(branch_id)
    query += " GROUP BY ps.payment_method;"
    return pd.read_sql(query, connection, params=params)


def get_branch_comparison(connection, role, branch_id):
    query = """
        SELECT b.branch_name,
               COALESCE(SUM(cs.gross_sales),0) AS total_gross,
               COALESCE(SUM(cs.received_amount),0) AS total_received,
               COALESCE(SUM(cs.pending_amount),0) AS total_pending
        FROM branches b LEFT JOIN customer_sales_table cs ON b.branch_id = cs.branch_id
    """
    params = []
    if role != "Super Admin":
        query += " WHERE b.branch_id = ?"
        params.append(branch_id)
    query += " GROUP BY b.branch_name ORDER BY total_gross DESC;"
    return pd.read_sql(query, connection, params=params)


def get_monthly_trend(connection, role, branch_id):
    query = "SELECT strftime('%Y-%m', date) AS month, COALESCE(SUM(gross_sales),0) AS total_sales FROM customer_sales_table"
    params = []
    if role != "Super Admin":
        query += " WHERE branch_id = ?"
        params.append(branch_id)
    query += " GROUP BY month ORDER BY month;"
    return pd.read_sql(query, connection, params=params)


def get_pending_collection_pct(connection, role, branch_id):
    kpi = get_kpis(connection, role, branch_id)
    total_gross = kpi["total_gross"] or 1
    pending_pct = (kpi["total_pending"] / total_gross) * 100
    return round(100 - pending_pct, 2), round(pending_pct, 2)


PREDEFINED_QUERIES = {
    "1. All records - customer_sales_table": "SELECT * FROM customer_sales_table;",
    "2. All records - branches": "SELECT * FROM branches;",
    "3. All records - payment_splits": "SELECT * FROM payment_splits;",
    "4. Sales with status = 'Open'": "SELECT * FROM customer_sales_table WHERE status = 'Open';",
    "5. Total gross sales (all branches)": "SELECT COALESCE(SUM(gross_sales),0) AS total_gross_sales FROM customer_sales_table;",
    "6. Total received amount (all sales)": "SELECT COALESCE(SUM(received_amount),0) AS total_received FROM customer_sales_table;",
    "7. Total pending amount (all sales)": "SELECT COALESCE(SUM(pending_amount),0) AS total_pending FROM customer_sales_table;",
    "8. Count of sales per branch": """
        SELECT b.branch_name, COUNT(cs.sale_id) AS total_sales
        FROM branches b LEFT JOIN customer_sales_table cs ON b.branch_id = cs.branch_id
        GROUP BY b.branch_name;
    """,
    "9. Sales details with branch name": """
        SELECT cs.sale_id, cs.name, cs.gross_sales, b.branch_name
        FROM customer_sales_table cs JOIN branches b ON cs.branch_id = b.branch_id;
    """,
    "10. Sales with total payment received (via payment_splits)": """
        SELECT cs.sale_id, cs.name, cs.gross_sales, COALESCE(SUM(ps.amount_paid),0) AS total_paid
        FROM customer_sales_table cs LEFT JOIN payment_splits ps ON cs.sale_id = ps.sale_id
        GROUP BY cs.sale_id;
    """,
    "11. Branch-wise total gross sales (JOIN + GROUP BY)": """
        SELECT b.branch_name, COALESCE(SUM(cs.gross_sales),0) AS total_gross_sales
        FROM branches b LEFT JOIN customer_sales_table cs ON b.branch_id = cs.branch_id
        GROUP BY b.branch_name ORDER BY total_gross_sales DESC;
    """,
    "12. Sales along with branch admin name": """
        SELECT cs.sale_id, cs.name, cs.gross_sales, b.branch_admin_name
        FROM customer_sales_table cs JOIN branches b ON cs.branch_id = b.branch_id;
    """,
    "13. Sales where pending amount > 5000": "SELECT * FROM customer_sales_table WHERE pending_amount > 5000;",
    "14. Top 3 highest gross sales": "SELECT * FROM customer_sales_table ORDER BY gross_sales DESC LIMIT 3;",
    "15. Branch with highest total gross sales": """
        SELECT b.branch_name, COALESCE(SUM(cs.gross_sales),0) AS total_gross_sales
        FROM branches b LEFT JOIN customer_sales_table cs ON b.branch_id = cs.branch_id
        GROUP BY b.branch_name ORDER BY total_gross_sales DESC LIMIT 1;
    """,
}


# ===========================================================================
# SESSION STATE + LOGIN
# ===========================================================================

if "logged_in" not in st.session_state:
    st.session_state["logged_in"] = False


def show_login():
    st.title("📊 Sales Intelligence Hub")
    st.subheader("Login")

    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Log in")

    if submitted:
        user_row = authenticate(username, password, connection)
        if user_row is None:
            st.error("Invalid username or password.")
        else:
            st.session_state["logged_in"] = True
            st.session_state["username"] = user_row["username"]
            st.session_state["role"] = user_row["role"]
            st.session_state["branch_id"] = None if user_row["branch_id"] is None else int(user_row["branch_id"])
            st.rerun()


# ===========================================================================
# DASHBOARD PAGES
# ===========================================================================

def page_dashboard(role, branch_id):
    st.title("📊 Dashboard Overview")
    kpi = get_kpis(connection, role, branch_id)
    col1, col2, col3 = st.columns(3)
    col1.metric("Total Gross Sales", f"₹{kpi['total_gross']:,.0f}")
    col2.metric("Total Received", f"₹{kpi['total_received']:,.0f}")
    col3.metric("Total Pending", f"₹{kpi['total_pending']:,.0f}")


def page_add_sale(role, branch_id):
    st.title("🧾 Add New Sale")
    branches_df = get_branches(connection)

    with st.form("add_sale_form"):
        if role == "Super Admin":
            branch_choice = st.selectbox(
                "Branch", options=branches_df["branch_id"],
                format_func=lambda bid: branches_df.loc[branches_df["branch_id"] == bid, "branch_name"].values[0],
            )
        else:
            branch_name = branches_df.loc[branches_df["branch_id"] == branch_id, "branch_name"].values[0]
            st.text_input("Branch", value=branch_name, disabled=True)
            branch_choice = branch_id

        sale_date = st.date_input("Sale date", value=datetime.date.today())
        name = st.text_input("Customer name")
        mobile_number = st.text_input("Mobile number")
        product_name = st.text_input("Product name")
        gross_sales = st.number_input("Gross sales amount", min_value=0.0, step=100.0)
        submitted = st.form_submit_button("Add Sale")

    if submitted:
        if not name or not mobile_number or not product_name or gross_sales <= 0:
            st.error("Please fill in all fields with valid values.")
        else:
            success, message, new_id = insert_sale(connection, branch_choice, sale_date.isoformat(), name, mobile_number, product_name, gross_sales)
            if success:
                st.success(message)
                st.write("Sale record (pending_amount filled automatically by the trigger):")
                st.dataframe(get_sale_row(connection, new_id), use_container_width=True)
            else:
                st.error(message)


def page_add_payment(role, branch_id):
    st.title("💰 Add Payment")
    open_sales = get_sales(connection, role, branch_id, status_filter="Open")

    if open_sales.empty:
        st.info("There are no open sales available to record a payment against.")
        return

    sale_options = open_sales.apply(
        lambda r: f"Sale #{r['sale_id']} — {r['name']} — Pending: ₹{r['pending_amount']:,.0f}", axis=1
    )
    selected_label = st.selectbox("Select sale", options=sale_options)
    selected_sale_id = int(open_sales.iloc[sale_options[sale_options == selected_label].index[0]]["sale_id"])

    with st.form("add_payment_form"):
        payment_date = st.date_input("Payment date", value=datetime.date.today())
        amount_paid = st.number_input("Amount paid", min_value=0.0, step=100.0)
        payment_method = st.selectbox("Payment method", options=["Cash", "UPI", "Card"])
        submitted = st.form_submit_button("Record Payment")

    if submitted:
        if amount_paid <= 0:
            st.error("Enter a valid payment amount.")
        elif role != "Super Admin" and not sale_belongs_to_branch(connection, selected_sale_id, branch_id):
            st.error("This sale does not belong to your branch.")
        else:
            success, message = insert_payment(connection, selected_sale_id, payment_date.isoformat(), amount_paid, payment_method)
            if success:
                st.success(message)
                st.dataframe(get_sale_row(connection, selected_sale_id), use_container_width=True)
            else:
                st.error(message)

    st.divider()
    st.subheader("Payment history for selected sale")
    st.dataframe(get_payment_history(connection, selected_sale_id), use_container_width=True)


def page_sales_report(role, branch_id):
    st.title("📋 Sales Report")
    col1, col2 = st.columns(2)

    with col1:
        if role == "Super Admin":
            branches_df = get_branches(connection)
            branch_choice = st.selectbox("Branch filter", options=["All"] + branches_df["branch_name"].tolist())
            if branch_choice == "All":
                eff_branch_id, eff_role = None, "Super Admin"
            else:
                eff_branch_id = int(branches_df.loc[branches_df["branch_name"] == branch_choice, "branch_id"].values[0])
                eff_role = "Admin"
        else:
            st.selectbox("Branch filter", options=[get_branch_name(connection, branch_id)], disabled=True)
            eff_branch_id, eff_role = branch_id, "Admin"

    with col2:
        status_choice = st.selectbox("Status filter", options=["All", "Open", "Close"])

    df = get_sales(connection, eff_role, eff_branch_id, status_filter=status_choice)
    st.write(f"Showing **{len(df)}** sales record(s).")
    st.dataframe(df, use_container_width=True)

    st.divider()
    st.subheader("Pending Payments Only")
    pending_df = df[df["pending_amount"] > 0]
    st.write(f"**{len(pending_df)}** sale(s) with an outstanding balance.")
    st.dataframe(pending_df, use_container_width=True)


def page_payment_methods(role, branch_id):
    st.title("💳 Payment Method Summary")
    summary_df = get_payment_method_summary(connection, role, branch_id)
    if summary_df.empty:
        st.info("No payments recorded yet.")
    else:
        st.dataframe(summary_df, use_container_width=True)
        st.bar_chart(summary_df.set_index("payment_method")["total_collected"])


def page_run_sql_queries(role):
    st.title("🧮 Run Predefined SQL Queries")
    if role != "Super Admin":
        st.warning("This analytical query bank is available to Super Admin only. "
                   "Use Insights & Reporting for your branch-specific analytics.")
        return

    query_name = st.selectbox("Choose a query", options=list(PREDEFINED_QUERIES.keys()))
    with st.expander("View SQL"):
        st.code(PREDEFINED_QUERIES[query_name].strip(), language="sql")

    if st.button("Run Query"):
        result_df = pd.read_sql(PREDEFINED_QUERIES[query_name], connection)
        st.write(f"**{len(result_df)}** row(s) returned.")
        st.dataframe(result_df, use_container_width=True)
        csv = result_df.to_csv(index=False).encode("utf-8")
        st.download_button("Download result as CSV", data=csv, file_name="query_result.csv", mime="text/csv")


def page_insights(role, branch_id):
    st.title("📈 Insights & Reporting")

    kpi = get_kpis(connection, role, branch_id)
    col1, col2, col3 = st.columns(3)
    col1.metric("Overall Revenue (Gross)", f"₹{kpi['total_gross']:,.0f}")
    col2.metric("Total Received", f"₹{kpi['total_received']:,.0f}")
    col3.metric("Total Pending", f"₹{kpi['total_pending']:,.0f}")

    collected_pct, pending_pct = get_pending_collection_pct(connection, role, branch_id)
    st.subheader("Collection Progress")
    st.progress(collected_pct / 100)
    st.write(f"**{collected_pct}%** collected, **{pending_pct}%** still pending.")

    st.divider()
    st.subheader("Branch-wise Sales Comparison")
    branch_df = get_branch_comparison(connection, role, branch_id)
    st.dataframe(branch_df, use_container_width=True)
    if len(branch_df) > 1:
        st.bar_chart(branch_df.set_index("branch_name")[["total_gross", "total_received", "total_pending"]])

    st.divider()
    st.subheader("Sales Trend (Monthly)")
    trend_df = get_monthly_trend(connection, role, branch_id)
    if trend_df.empty:
        st.info("Not enough data yet to show a trend.")
    else:
        st.line_chart(trend_df.set_index("month")["total_sales"])

    st.divider()
    st.subheader("Payment Method Analysis")
    pm_df = get_payment_method_summary(connection, role, branch_id)
    if pm_df.empty:
        st.info("No payments recorded yet.")
    else:
        st.bar_chart(pm_df.set_index("payment_method")["total_collected"])


# ===========================================================================
# MAIN ROUTING
# ===========================================================================

if not st.session_state["logged_in"]:
    show_login()
else:
    role = st.session_state["role"]
    branch_id = st.session_state["branch_id"]

    with st.sidebar:
        st.caption(f"DB: {DB_PATH}")
        st.write(f"Logged in as **{st.session_state['username']}**")
        st.write(f"Role: **{role}**")
        st.write(f"Branch: **{'All branches' if branch_id is None else get_branch_name(connection, branch_id)}**")

        page = st.radio(
            "Navigate",
            ["Dashboard", "Add Sale", "Add Payment", "Sales Report",
             "Payment Methods", "Run SQL Queries", "Insights & Reporting"]
        )

        if st.button("Log out"):
            for key in ["logged_in", "username", "role", "branch_id"]:
                st.session_state.pop(key, None)
            st.rerun()

    if page == "Dashboard":
        page_dashboard(role, branch_id)
    elif page == "Add Sale":
        page_add_sale(role, branch_id)
    elif page == "Add Payment":
        page_add_payment(role, branch_id)
    elif page == "Sales Report":
        page_sales_report(role, branch_id)
    elif page == "Payment Methods":
        page_payment_methods(role, branch_id)
    elif page == "Run SQL Queries":
        page_run_sql_queries(role)
    elif page == "Insights & Reporting":
        page_insights(role, branch_id)
