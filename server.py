import os, json, hashlib
from typing import Optional
from datetime import datetime, date
from mcp.server.fastmcp import FastMCP
from sqlalchemy import create_engine, inspect, text
from sshtunnel import SSHTunnelForwarder

### Database ###
# read from os
use_ssh = os.environ.get('USE_SSH')
ssh_host = os.environ.get('SSH_HOST')
ssh_port = int(os.environ.get('SSH_PORT', 22))
ssh_username = os.environ.get('SSH_USERNAME')
private_key = os.environ.get('SSH_PRIVATE_KEY')

db_host = os.environ.get('DB_HOST')
db_port = int(os.environ.get('DB_PORT'))
db_username = os.environ.get('DB_USERNAME')
db_password = os.environ.get('DB_PASSWORD')
db_name = os.environ.get('DB_NAME')


def get_engine(readonly=True, ssh_host=None, ssh_port=None, ssh_username=None, private_key=None, db_host=None, db_port=None, db_username=None, db_password=None, db_name=None):
    if use_ssh == 'true':
        tunnel = SSHTunnelForwarder(
            (ssh_host, ssh_port),
            ssh_username=ssh_username,
            ssh_pkey=private_key,
            remote_bind_address=(db_host, db_port)
        )
        tunnel.start()
        # After the tunnel is established, create a database engine
        local_port = tunnel.local_bind_port
        connection_string = f"postgresql://{db_username}:{db_password}@localhost:{local_port}/{db_name}"
        engine = create_engine(connection_string, isolation_level='AUTOCOMMIT', execution_options={'readonly': readonly})
        return tunnel, engine
    else:
        connection_string = f"postgresql://{db_username}:{db_password}@{db_host}:{db_port}/{db_name}"
        engine = create_engine(connection_string, isolation_level='AUTOCOMMIT', execution_options={'readonly': readonly})
        return None, engine

def get_db_info(ssh_host, ssh_port, ssh_username, private_key, db_host, db_port, db_username, db_password, db_name):
    tunnel, engine = get_engine(
        readonly=True,
        ssh_host=ssh_host,
        ssh_port=ssh_port,
        ssh_username=ssh_username,
        private_key=private_key,
        db_host=db_host,
        db_port=db_port,
        db_username=db_username,
        db_password=db_password,
        db_name=db_name
    )
    with engine.connect() as conn:
        url = engine.url
        obj = (f"Connected to {engine.dialect.name} "
                f"version {'.'.join(str(x) for x in engine.dialect.server_version_info)} "
                f"database '{url.database}' on {url.host} "
                f"as user '{url.username}'")
        if tunnel:
            tunnel.stop()
        return obj

### Constants ###

DB_INFO = get_db_info(ssh_host, ssh_port, ssh_username, private_key, db_host, db_port, db_username, db_password, db_name)
EXECUTE_QUERY_MAX_CHARS = int(os.environ.get('EXECUTE_QUERY_MAX_CHARS', 4000))
CLAUDE_FILES_PATH = os.environ.get('CLAUDE_LOCAL_FILES_PATH')

### MCP ###

mcp = FastMCP("MCP Alchemy")

@mcp.tool(description=f"Return all table names in the database separated by comma. {DB_INFO}")
def all_table_names() -> str:
    tunnel, engine = get_engine()
    inspector = inspect(engine)
    if tunnel:
        tunnel.stop()
    return ", ".join(inspector.get_table_names())

@mcp.tool(
    description=f"Return all table names in the database containing the substring 'q' separated by comma. {DB_INFO}"
)
def filter_table_names(q: str) -> str:
    tunnel, engine = get_engine()
    inspector = inspect(engine)
    if tunnel:
        tunnel.stop()
    return ", ".join(x for x in inspector.get_table_names() if q in x)

@mcp.tool(description=f"Returns schema and relation information for the given tables. {DB_INFO}")
def schema_definitions(table_names: list[str]) -> str:
    def format(inspector, table_name):
        columns = inspector.get_columns(table_name)
        foreign_keys = inspector.get_foreign_keys(table_name)
        primary_keys = set(inspector.get_pk_constraint(table_name)["constrained_columns"])
        result = [f"{table_name}:"]

        # Process columns
        show_key_only = {"nullable", "autoincrement"}
        for column in columns:
            if "comment" in column:
                del column["comment"]
            name = column.pop("name")
            column_parts = (["primary key"] if name in primary_keys else []) + [str(
                column.pop("type"))] + [k if k in show_key_only else f"{k}={v}" for k, v in column.items() if v]
            result.append(f"    {name}: " + ", ".join(column_parts))

        # Process relationships
        if foreign_keys:
            result.extend(["", "    Relationships:"])
            for fk in foreign_keys:
                constrained_columns = ", ".join(fk['constrained_columns'])
                referred_table = fk['referred_table']
                referred_columns = ", ".join(fk['referred_columns'])
                result.append(f"      {constrained_columns} -> {referred_table}.{referred_columns}")

        return "\n".join(result)

    tunnel, engine = get_engine()
    inspector = inspect(engine)
    if tunnel:
        tunnel.stop()
    return "\n".join(format(inspector, table_name) for table_name in table_names)

def execute_query_description():
    parts = [
        f"Execute a SQL query and return results in a readable format. Results will be truncated after {EXECUTE_QUERY_MAX_CHARS} characters."
    ]
    if CLAUDE_FILES_PATH:
        parts.append("Claude Desktop may fetch the full result set via an url for analysis and artifacts.")
    parts.append(DB_INFO)
    return " ".join(parts)

@mcp.tool(description=execute_query_description())
def execute_query(query: str, params: Optional[dict] = None) -> str:
    def format_value(val):
        """Format a value for display, handling None and datetime types"""
        if val is None:
            return "NULL"
        if isinstance(val, (datetime, date)):
            return val.isoformat()
        return str(val)

    def format_results(columns, rows):
        """Format rows in a clean vertical format"""
        output = ""
        curr_size, row_displayed = 0, 0

        for i, row in enumerate(rows, 1):
            line = f"{i}. row\n"
            for col, val in zip(columns, row):
                line += f"{col}: {format_value(val)}\n"
            line += "\n"
            curr_size += len(line)

            if curr_size > EXECUTE_QUERY_MAX_CHARS:
                break
            output += line
            row_displayed = i

        return row_displayed, output

    def save_full_results(rows, columns):
        """Save complete result set for Claude if configured"""
        if not CLAUDE_FILES_PATH:
            return ""

        def serialize_row(row):
            return [format_value(val) for val in row]

        data = [serialize_row(row) for row in rows]
        file_hash = hashlib.sha256(json.dumps(data).encode()).hexdigest()
        file_name = f"{file_hash}.json"

        with open(os.path.join(CLAUDE_FILES_PATH, file_name), 'w') as f:
            json.dump(data, f)

        return (
            f"\nFull result set url: https://cdn.jsdelivr.net/pyodide/claude-local-files/{file_name}"
            " (format: [[row1_value1, row1_value2, ...], [row2_value1, row2_value2, ...], ...]])"
            " (ALWAYS prefer fetching this url in artifacts instead of hardcoding the values if at all possible)")

    try:
        tunnel, engine = get_engine(readonly=True)
        with engine.connect() as connection:
            result = connection.execute(text(query), params or {})

            if not result.returns_rows:
                return f"Success: {result.rowcount} rows affected"

            columns = result.keys()
            all_rows = result.fetchall()

            if not all_rows:
                return "No rows returned"

            # Format results and handle truncation if needed
            row_displayed, output = format_results(columns, all_rows)

            # Add summary and full results link
            output += f"\nResult: {len(all_rows)} rows"
            if row_displayed < len(all_rows):
                output += " (output truncated)"

            if full_results := save_full_results(all_rows, columns):
                output += full_results

            if tunnel:
                tunnel.stop()
            return output
    except Exception as e:
        if tunnel:
            tunnel.stop()
        return f"Error: {str(e)}"


def main():
    mcp.run()


if __name__ == "__main__":
    main()
