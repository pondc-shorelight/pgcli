import traceback
import logging
import psycopg2
import psycopg2.extras
import psycopg2.errorcodes
import psycopg2.extensions as ext
import sqlparse
import pgspecial as special
import select
from psycopg2.extensions import POLL_OK, POLL_READ, POLL_WRITE, make_dsn
from .packages.parseutils.meta import FunctionMetadata, ForeignKey
from .encodingutils import unicode2utf8, PY2, utf8tounicode

_logger = logging.getLogger(__name__)

# Cast all database input to unicode automatically.
# See http://initd.org/psycopg/docs/usage.html#unicode-handling for more info.
ext.register_type(ext.UNICODE)
ext.register_type(ext.UNICODEARRAY)
ext.register_type(ext.new_type((705,), "UNKNOWN", ext.UNICODE))
# See https://github.com/dbcli/pgcli/issues/426 for more details.
# This registers a unicode type caster for datatype 'RECORD'.
ext.register_type(ext.new_type((2249,), "RECORD", ext.UNICODE))

# Cast bytea fields to text. By default, this will render as hex strings with
# Postgres 9+ and as escaped binary in earlier versions.
ext.register_type(ext.new_type((17,), "BYTEA_TEXT", psycopg2.STRING))

# TODO: Get default timeout from pgclirc?
_WAIT_SELECT_TIMEOUT = 1


def _wait_select(conn):
    """
        copy-pasted from psycopg2.extras.wait_select
        the default implementation doesn't define a timeout in the select calls
    """
    while 1:
        try:
            state = conn.poll()
            if state == POLL_OK:
                break
            elif state == POLL_READ:
                select.select([conn.fileno()], [], [], _WAIT_SELECT_TIMEOUT)
            elif state == POLL_WRITE:
                select.select([], [conn.fileno()], [], _WAIT_SELECT_TIMEOUT)
            else:
                raise conn.OperationalError("bad state from poll: %s" % state)
        except KeyboardInterrupt:
            conn.cancel()
            # the loop will be broken by a server error
            continue
        except select.error as e:
            errno = e.args[0]
            if errno != 4:
                raise


# When running a query, make pressing CTRL+C raise a KeyboardInterrupt
# See http://initd.org/psycopg/articles/2014/07/20/cancelling-postgresql-statements-python/
# See also https://github.com/psycopg/psycopg2/issues/468
ext.set_wait_callback(_wait_select)


def register_date_typecasters(connection):
    """
    Casts date and timestamp values to string, resolves issues with out of
    range dates (e.g. BC) which psycopg2 can't handle
    """

    def cast_date(value, cursor):
        return value

    cursor = connection.cursor()
    cursor.execute("SELECT NULL::date")
    date_oid = cursor.description[0][1]
    cursor.execute("SELECT NULL::timestamp")
    timestamp_oid = cursor.description[0][1]
    cursor.execute("SELECT NULL::timestamp with time zone")
    timestamptz_oid = cursor.description[0][1]
    oids = (date_oid, timestamp_oid, timestamptz_oid)
    new_type = psycopg2.extensions.new_type(oids, "DATE", cast_date)
    psycopg2.extensions.register_type(new_type)


def register_json_typecasters(conn, loads_fn):
    """Set the function for converting JSON data for a connection.

    Use the supplied function to decode JSON data returned from the database
    via the given connection. The function should accept a single argument of
    the data as a string encoded in the database's character encoding.
    psycopg2's default handler for JSON data is json.loads.
    http://initd.org/psycopg/docs/extras.html#json-adaptation

    This function attempts to register the typecaster for both JSON and JSONB
    types.

    Returns a set that is a subset of {'json', 'jsonb'} indicating which types
    (if any) were successfully registered.
    """
    available = set()

    for name in ["json", "jsonb"]:
        try:
            psycopg2.extras.register_json(conn, loads=loads_fn, name=name)
            available.add(name)
        except psycopg2.ProgrammingError:
            pass

    return available


def register_hstore_typecaster(conn):
    """
    Instead of using register_hstore() which converts hstore into a python
    dict, we query the 'oid' of hstore which will be different for each
    database and register a type caster that converts it to unicode.
    http://initd.org/psycopg/docs/extras.html#psycopg2.extras.register_hstore
    """
    with conn.cursor() as cur:
        try:
            cur.execute(
                "select t.oid FROM pg_type t WHERE t.typname = 'hstore' and t.typisdefined"
            )
            oid = cur.fetchone()[0]
            ext.register_type(ext.new_type((oid,), "HSTORE", ext.UNICODE))
        except Exception:
            pass


class PGExecute(object):

    # The boolean argument to the current_schemas function indicates whether
    # implicit schemas, e.g. pg_catalog
    search_path_query = """
        SELECT * FROM unnest(current_schemas(true))"""

    schemata_query = """
        SELECT  nspname
        FROM    pg_catalog.pg_namespace
        ORDER BY 1 """

    tables_query = """
        SELECT  n.nspname schema_name,
                c.relname table_name
        FROM    pg_catalog.pg_class c
                LEFT JOIN pg_catalog.pg_namespace n
                    ON n.oid = c.relnamespace
        WHERE   c.relkind = ANY(%s)
        ORDER BY 1,2;"""

    databases_query = """
        SELECT d.datname
        FROM pg_catalog.pg_database d
        ORDER BY 1"""

    full_databases_query = """
        SELECT d.datname as "Name",
            pg_catalog.pg_get_userbyid(d.datdba) as "Owner",
            pg_catalog.pg_encoding_to_char(d.encoding) as "Encoding",
            d.datcollate as "Collate",
            d.datctype as "Ctype",
            pg_catalog.array_to_string(d.datacl, E'\n') AS "Access privileges"
        FROM pg_catalog.pg_database d
        ORDER BY 1"""

    socket_directory_query = """
        SELECT setting
        FROM pg_settings
        WHERE name = 'unix_socket_directories'
    """

    view_definition_query = """
        WITH v AS (SELECT %s::pg_catalog.regclass::pg_catalog.oid AS v_oid)
        SELECT nspname, relname, relkind,
               pg_catalog.pg_get_viewdef(c.oid, true),
               array_remove(array_remove(c.reloptions,'check_option=local'),
                            'check_option=cascaded') AS reloptions,
               CASE
                 WHEN 'check_option=local' = ANY (c.reloptions) THEN 'LOCAL'::text
                 WHEN 'check_option=cascaded' = ANY (c.reloptions) THEN 'CASCADED'::text
                 ELSE NULL
               END AS checkoption
        FROM pg_catalog.pg_class c
        LEFT JOIN pg_catalog.pg_namespace n ON (c.relnamespace = n.oid)
        JOIN v ON (c.oid = v.v_oid)"""

    function_definition_query = """
        WITH f AS
            (SELECT %s::pg_catalog.regproc::pg_catalog.oid AS f_oid)
        SELECT pg_catalog.pg_get_functiondef(f.f_oid)
        FROM f"""

    version_query = "SELECT version();"

    def __init__(
        self,
        database=None,
        user=None,
        password=None,
        host=None,
        port=None,
        dsn=None,
        **kwargs
    ):
        self._conn_params = {}
        self.conn = None
        self.dbname = None
        self.user = None
        self.password = None
        self.host = None
        self.port = None
        self.server_version = None
        self.extra_args = None
        self.connect(database, user, password, host, port, dsn, **kwargs)
        self.reset_expanded = None

    def copy(self):
        """Returns a clone of the current executor."""
        return self.__class__(**self._conn_params)

    def get_server_version(self, cursor):
        _logger.debug("Version Query. sql: %r", self.version_query)
        cursor.execute(self.version_query)
        result = cursor.fetchone()
        server_version = ""
        if result:
            # full version string looks like this:
            # PostgreSQL 10.3 on x86_64-apple-darwin17.3.0, compiled by Apple LLVM version 9.0.0 (clang-900.0.39.2), 64-bit  # noqa
            # let's only retrieve version number
            version_parts = result[0].split()
            server_version = version_parts[1]
        return server_version

    def connect(
        self,
        database=None,
        user=None,
        password=None,
        host=None,
        port=None,
        dsn=None,
        **kwargs
    ):

        conn_params = self._conn_params.copy()

        new_params = {
            "database": database,
            "user": user,
            "password": password,
            "host": host,
            "port": port,
            "dsn": dsn,
        }
        new_params.update(kwargs)

        if new_params["dsn"]:
            new_params = {"dsn": new_params["dsn"], "password": new_params["password"]}

            if new_params["password"]:
                new_params["dsn"] = make_dsn(
                    new_params["dsn"], password=new_params.pop("password")
                )

        conn_params.update({k: unicode2utf8(v) for k, v in new_params.items() if v})

        conn = psycopg2.connect(**conn_params)
        cursor = conn.cursor()
        conn.set_client_encoding("utf8")

        self._conn_params = conn_params
        if self.conn:
            self.conn.close()
        self.conn = conn
        self.conn.autocommit = True

        # When we connect using a DSN, we don't really know what db,
        # user, etc. we connected to. Let's read it.
        # Note: moved this after setting autocommit because of #664.
        # TODO: use actual connection info from psycopg2.extensions.Connection.info as psycopg>2.8 is available and required dependency  # noqa
        dsn_parameters = conn.get_dsn_parameters()

        self.dbname = dsn_parameters.get("dbname")
        self.user = dsn_parameters.get("user")
        self.password = password
        self.host = dsn_parameters.get("host")
        self.port = dsn_parameters.get("port")
        self.extra_args = kwargs

        if not self.host:
            self.host = self.get_socket_directory()

        cursor.execute("SHOW ALL")
        db_parameters = dict(name_val_desc[:2] for name_val_desc in cursor.fetchall())

        pid = self._select_one(cursor, "select pg_backend_pid()")[0]
        self.pid = pid
        self.superuser = db_parameters.get("is_superuser") == "1"

        self.server_version = self.get_server_version(cursor)

        register_date_typecasters(conn)
        register_json_typecasters(self.conn, self._json_typecaster)
        register_hstore_typecaster(self.conn)

    @property
    def short_host(self):
        if "," in self.host:
            host, _, _ = self.host.partition(",")
        else:
            host = self.host
        short_host, _, _ = host.partition(".")
        return short_host

    def _select_one(self, cur, sql):
        """
        Helper method to run a select and retrieve a single field value
        :param cur: cursor
        :param sql: string
        :return: string
        """
        cur.execute(sql)
        return cur.fetchone()

    def _json_typecaster(self, json_data):
        """Interpret incoming JSON data as a string.

        The raw data is decoded using the connection's encoding, which defaults
        to the database's encoding.

        See http://initd.org/psycopg/docs/connection.html#connection.encoding
        """

        if PY2:
            return json_data.decode(self.conn.encoding)
        else:
            return json_data

    def failed_transaction(self):
        status = self.conn.get_transaction_status()
        return status == ext.TRANSACTION_STATUS_INERROR

    def valid_transaction(self):
        status = self.conn.get_transaction_status()
        return (
            status == ext.TRANSACTION_STATUS_ACTIVE
            or status == ext.TRANSACTION_STATUS_INTRANS
        )

    def run(
        self, statement, pgspecial=None, exception_formatter=None, on_error_resume=False
    ):
        """Execute the sql in the database and return the results.

        :param statement: A string containing one or more sql statements
        :param pgspecial: PGSpecial object
        :param exception_formatter: A callable that accepts an Exception and
               returns a formatted (title, rows, headers, status) tuple that can
               act as a query result. If an exception_formatter is not supplied,
               psycopg2 exceptions are always raised.
        :param on_error_resume: Bool. If true, queries following an exception
               (assuming exception_formatter has been supplied) continue to
               execute.

        :return: Generator yielding tuples containing
                 (title, rows, headers, status, query, success, is_special)
        """

        # Remove spaces and EOL
        statement = statement.strip()
        if not statement:  # Empty string
            yield (None, None, None, None, statement, False, False)

        # Split the sql into separate queries and run each one.
        for sql in sqlparse.split(statement):
            # Remove spaces, eol and semi-colons.
            sql = sql.rstrip(";")
            if not sql:
                continue

            try:
                if pgspecial:
                    # \G is treated specially since we have to set the expanded output.
                    if sql.endswith("\\G"):
                        if not pgspecial.expanded_output:
                            pgspecial.expanded_output = True
                            self.reset_expanded = True
                        sql = sql[:-2].strip()

                    # First try to run each query as special
                    _logger.debug("Trying a pgspecial command. sql: %r", sql)
                    try:
                        cur = self.conn.cursor()
                    except psycopg2.InterfaceError:
                        # edge case when connection is already closed, but we
                        # don't need cursor for special_cmd.arg_type == NO_QUERY.
                        # See https://github.com/dbcli/pgcli/issues/1014.
                        cur = None
                    try:
                        for result in pgspecial.execute(cur, sql):
                            # e.g. execute_from_file already appends these
                            if len(result) < 7:
                                yield result + (sql, True, True)
                            else:
                                yield result
                        continue
                    except special.CommandNotFound:
                        pass

                # Not a special command, so execute as normal sql
                yield self.execute_normal_sql(sql) + (sql, True, False)
            except psycopg2.DatabaseError as e:
                _logger.error("sql: %r, error: %r", sql, e)
                _logger.error("traceback: %r", traceback.format_exc())

                if self._must_raise(e) or not exception_formatter:
                    raise

                yield None, None, None, exception_formatter(e), sql, False, False

                if not on_error_resume:
                    break
            finally:
                if self.reset_expanded:
                    pgspecial.expanded_output = False
                    self.reset_expanded = None

    def _must_raise(self, e):
        """Return true if e is an error that should not be caught in ``run``.

        An uncaught error will prompt the user to reconnect; as long as we
        detect that the connection is stil open, we catch the error, as
        reconnecting won't solve that problem.

        :param e: DatabaseError. An exception raised while executing a query.

        :return: Bool. True if ``run`` must raise this exception.

        """
        return self.conn.closed != 0

    def execute_normal_sql(self, split_sql):
        """Returns tuple (title, rows, headers, status)"""
        _logger.debug("Regular sql statement. sql: %r", split_sql)
        cur = self.conn.cursor()
        cur.execute(split_sql)

        # conn.notices persist between queies, we use pop to clear out the list
        title = ""
        while len(self.conn.notices) > 0:
            title = utf8tounicode(self.conn.notices.pop()) + title

        # cur.description will be None for operations that do not return
        # rows.
        if cur.description:
            headers = [x[0] for x in cur.description]
            return title, cur, headers, cur.statusmessage
        else:
            _logger.debug("No rows in result.")
            return title, None, None, cur.statusmessage

    def search_path(self):
        """Returns the current search path as a list of schema names"""

        try:
            with self.conn.cursor() as cur:
                _logger.debug("Search path query. sql: %r", self.search_path_query)
                cur.execute(self.search_path_query)
                return [x[0] for x in cur.fetchall()]
        except psycopg2.ProgrammingError:
            fallback = "SELECT * FROM current_schemas(true)"
            with self.conn.cursor() as cur:
                _logger.debug("Search path query. sql: %r", fallback)
                cur.execute(fallback)
                return cur.fetchone()[0]

    def view_definition(self, spec):
        """Returns the SQL defining views described by `spec`"""

        template = "CREATE OR REPLACE {6} VIEW {0}.{1} AS \n{3}"
        # 2: relkind, v or m (materialized)
        # 4: reloptions, null
        # 5: checkoption: local or cascaded
        with self.conn.cursor() as cur:
            sql = self.view_definition_query
            _logger.debug("View Definition Query. sql: %r\nspec: %r", sql, spec)
            try:
                cur.execute(sql, (spec,))
            except psycopg2.ProgrammingError:
                raise RuntimeError("View {} does not exist.".format(spec))
            result = cur.fetchone()
            view_type = "MATERIALIZED" if result[2] == "m" else ""
            return template.format(*result + (view_type,))

    def function_definition(self, spec):
        """Returns the SQL defining functions described by `spec`"""

        with self.conn.cursor() as cur:
            sql = self.function_definition_query
            _logger.debug("Function Definition Query. sql: %r\nspec: %r", sql, spec)
            try:
                cur.execute(sql, (spec,))
                result = cur.fetchone()
                return result[0]
            except psycopg2.ProgrammingError:
                raise RuntimeError("Function {} does not exist.".format(spec))

    def schemata(self):
        """Returns a list of schema names in the database"""

        with self.conn.cursor() as cur:
            _logger.debug("Schemata Query. sql: %r", self.schemata_query)
            cur.execute(self.schemata_query)
            return [x[0] for x in cur.fetchall()]

    def _relations(self, kinds=("r", "v", "m")):
        """Get table or view name metadata

        :param kinds: list of postgres relkind filters:
                'r' - table
                'v' - view
                'm' - materialized view
        :return: (schema_name, rel_name) tuples
        """

        with self.conn.cursor() as cur:
            sql = cur.mogrify(self.tables_query, [kinds])
            _logger.debug("Tables Query. sql: %r", sql)
            cur.execute(sql)
            for row in cur:
                yield row

    def tables(self):
        """Yields (schema_name, table_name) tuples"""
        for row in self._relations(kinds=["r"]):
            yield row

    def views(self):
        """Yields (schema_name, view_name) tuples.

            Includes both views and and materialized views
        """
        for row in self._relations(kinds=["v", "m"]):
            yield row

    def _columns(self, kinds=("r", "v", "m")):
        """Get column metadata for tables and views

        :param kinds: kinds: list of postgres relkind filters:
                'r' - table
                'v' - view
                'm' - materialized view
        :return: list of (schema_name, relation_name, column_name, column_type) tuples
        """

        if self.conn.server_version >= 80400:
            columns_query = """
                SELECT  nsp.nspname schema_name,
                        cls.relname table_name,
                        att.attname column_name,
                        att.atttypid::regtype::text type_name,
                        att.atthasdef AS has_default,
                        pg_catalog.pg_get_expr(def.adbin, def.adrelid, true) as default
                FROM    pg_catalog.pg_attribute att
                        INNER JOIN pg_catalog.pg_class cls
                            ON att.attrelid = cls.oid
                        INNER JOIN pg_catalog.pg_namespace nsp
                            ON cls.relnamespace = nsp.oid
                        LEFT OUTER JOIN pg_attrdef def
                            ON def.adrelid = att.attrelid
                            AND def.adnum = att.attnum
                WHERE   cls.relkind = ANY(%s)
                        AND NOT att.attisdropped
                        AND att.attnum  > 0
                ORDER BY 1, 2, att.attnum"""
        else:
            columns_query = """
                SELECT  nsp.nspname schema_name,
                        cls.relname table_name,
                        att.attname column_name,
                        typ.typname type_name,
                        NULL AS has_default,
                        NULL AS default
                FROM    pg_catalog.pg_attribute att
                        INNER JOIN pg_catalog.pg_class cls
                            ON att.attrelid = cls.oid
                        INNER JOIN pg_catalog.pg_namespace nsp
                            ON cls.relnamespace = nsp.oid
                        INNER JOIN pg_catalog.pg_type typ
                            ON typ.oid = att.atttypid
                WHERE   cls.relkind = ANY(%s)
                        AND NOT att.attisdropped
                        AND att.attnum  > 0
                ORDER BY 1, 2, att.attnum"""

        with self.conn.cursor() as cur:
            sql = cur.mogrify(columns_query, [kinds])
            _logger.debug("Columns Query. sql: %r", sql)
            cur.execute(sql)
            for row in cur:
                yield row

    def table_columns(self):
        for row in self._columns(kinds=["r"]):
            yield row

    def view_columns(self):
        for row in self._columns(kinds=["v", "m"]):
            yield row

    def databases(self):
        with self.conn.cursor() as cur:
            _logger.debug("Databases Query. sql: %r", self.databases_query)
            cur.execute(self.databases_query)
            return [x[0] for x in cur.fetchall()]

    def full_databases(self):
        with self.conn.cursor() as cur:
            _logger.debug("Databases Query. sql: %r", self.full_databases_query)
            cur.execute(self.full_databases_query)
            headers = [x[0] for x in cur.description]
            return cur.fetchall(), headers, cur.statusmessage

    def get_socket_directory(self):
        with self.conn.cursor() as cur:
            _logger.debug(
                "Socket directory Query. sql: %r", self.socket_directory_query
            )
            cur.execute(self.socket_directory_query)
            result = cur.fetchone()
            return result[0] if result else ""

    def foreignkeys(self):
        """Yields ForeignKey named tuples"""

        if self.conn.server_version < 90000:
            return

        with self.conn.cursor() as cur:
            query = """
                SELECT s_p.nspname AS parentschema,
                       t_p.relname AS parenttable,
                       unnest((
                        select
                            array_agg(attname ORDER BY i)
                        from
                            (select unnest(confkey) as attnum, generate_subscripts(confkey, 1) as i) x
                            JOIN pg_catalog.pg_attribute c USING(attnum)
                            WHERE c.attrelid = fk.confrelid
                        )) AS parentcolumn,
                       s_c.nspname AS childschema,
                       t_c.relname AS childtable,
                       unnest((
                        select
                            array_agg(attname ORDER BY i)
                        from
                            (select unnest(conkey) as attnum, generate_subscripts(conkey, 1) as i) x
                            JOIN pg_catalog.pg_attribute c USING(attnum)
                            WHERE c.attrelid = fk.conrelid
                        )) AS childcolumn
                FROM pg_catalog.pg_constraint fk
                JOIN pg_catalog.pg_class      t_p ON t_p.oid = fk.confrelid
                JOIN pg_catalog.pg_namespace  s_p ON s_p.oid = t_p.relnamespace
                JOIN pg_catalog.pg_class      t_c ON t_c.oid = fk.conrelid
                JOIN pg_catalog.pg_namespace  s_c ON s_c.oid = t_c.relnamespace
                WHERE fk.contype = 'f';
                """
            _logger.debug("Functions Query. sql: %r", query)
            cur.execute(query)
            for row in cur:
                yield ForeignKey(*row)

    def functions(self):
        """Yields FunctionMetadata named tuples"""

        if self.conn.server_version >= 110000:
            query = """
                SELECT n.nspname schema_name,
                        p.proname func_name,
                        p.proargnames,
                        COALESCE(proallargtypes::regtype[], proargtypes::regtype[])::text[],
                        p.proargmodes,
                        prorettype::regtype::text return_type,
                        p.prokind = 'a' is_aggregate,
                        p.prokind = 'w' is_window,
                        p.proretset is_set_returning,
                        d.deptype = 'e' is_extension,
                        pg_get_expr(proargdefaults, 0) AS arg_defaults
                FROM pg_catalog.pg_proc p
                        INNER JOIN pg_catalog.pg_namespace n
                            ON n.oid = p.pronamespace
                LEFT JOIN pg_depend d ON d.objid = p.oid and d.deptype = 'e'
                WHERE p.prorettype::regtype != 'trigger'::regtype
                ORDER BY 1, 2
                """
        elif self.conn.server_version > 90000:
            query = """
                SELECT n.nspname schema_name,
                        p.proname func_name,
                        p.proargnames,
                        COALESCE(proallargtypes::regtype[], proargtypes::regtype[])::text[],
                        p.proargmodes,
                        prorettype::regtype::text return_type,
                        p.proisagg is_aggregate,
                        p.proiswindow is_window,
                        p.proretset is_set_returning,
                        d.deptype = 'e' is_extension,
                        pg_get_expr(proargdefaults, 0) AS arg_defaults
                FROM pg_catalog.pg_proc p
                        INNER JOIN pg_catalog.pg_namespace n
                            ON n.oid = p.pronamespace
                LEFT JOIN pg_depend d ON d.objid = p.oid and d.deptype = 'e'
                WHERE p.prorettype::regtype != 'trigger'::regtype
                ORDER BY 1, 2
                """
        elif self.conn.server_version >= 80400:
            query = """
                SELECT n.nspname schema_name,
                        p.proname func_name,
                        p.proargnames,
                        COALESCE(proallargtypes::regtype[], proargtypes::regtype[])::text[],
                        p.proargmodes,
                        prorettype::regtype::text,
                        p.proisagg is_aggregate,
                        false is_window,
                        p.proretset is_set_returning,
                        d.deptype = 'e' is_extension,
                        NULL AS arg_defaults
                FROM pg_catalog.pg_proc p
                        INNER JOIN pg_catalog.pg_namespace n
                            ON n.oid = p.pronamespace
                LEFT JOIN pg_depend d ON d.objid = p.oid and d.deptype = 'e'
                WHERE p.prorettype::regtype != 'trigger'::regtype
                ORDER BY 1, 2
                """
        else:
            query = """
                SELECT n.nspname schema_name,
                        p.proname func_name,
                        p.proargnames,
                        NULL arg_types,
                        NULL arg_modes,
                        '' ret_type,
                        p.proisagg is_aggregate,
                        false is_window,
                        p.proretset is_set_returning,
                        d.deptype = 'e' is_extension,
                        NULL AS arg_defaults
                FROM pg_catalog.pg_proc p
                        INNER JOIN pg_catalog.pg_namespace n
                            ON n.oid = p.pronamespace
                LEFT JOIN pg_depend d ON d.objid = p.oid and d.deptype = 'e'
                WHERE p.prorettype::regtype != 'trigger'::regtype
                ORDER BY 1, 2
                """

        with self.conn.cursor() as cur:
            _logger.debug("Functions Query. sql: %r", query)
            cur.execute(query)
            for row in cur:
                yield FunctionMetadata(*row)

    def datatypes(self):
        """Yields tuples of (schema_name, type_name)"""

        with self.conn.cursor() as cur:
            if self.conn.server_version > 90000:
                query = """
                    SELECT n.nspname schema_name,
                           t.typname type_name
                    FROM   pg_catalog.pg_type t
                           INNER JOIN pg_catalog.pg_namespace n
                              ON n.oid = t.typnamespace
                    WHERE ( t.typrelid = 0  -- non-composite types
                            OR (  -- composite type, but not a table
                                  SELECT c.relkind = 'c'
                                  FROM pg_catalog.pg_class c
                                  WHERE c.oid = t.typrelid
                                )
                          )
                          AND NOT EXISTS( -- ignore array types
                                SELECT  1
                                FROM    pg_catalog.pg_type el
                                WHERE   el.oid = t.typelem AND el.typarray = t.oid
                              )
                          AND n.nspname <> 'pg_catalog'
                          AND n.nspname <> 'information_schema'
                    ORDER BY 1, 2;
                    """
            else:
                query = """
                    SELECT n.nspname schema_name,
                      pg_catalog.format_type(t.oid, NULL) type_name
                    FROM pg_catalog.pg_type t
                         LEFT JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
                    WHERE (t.typrelid = 0 OR (SELECT c.relkind = 'c' FROM pg_catalog.pg_class c WHERE c.oid = t.typrelid))
                      AND t.typname !~ '^_'
                          AND n.nspname <> 'pg_catalog'
                          AND n.nspname <> 'information_schema'
                      AND pg_catalog.pg_type_is_visible(t.oid)
                    ORDER BY 1, 2;
                """
            _logger.debug("Datatypes Query. sql: %r", query)
            cur.execute(query)
            for row in cur:
                yield row

    def casing(self):
        """Yields the most common casing for names used in db functions"""
        with self.conn.cursor() as cur:
            query = r"""
          WITH Words AS (
                SELECT regexp_split_to_table(prosrc, '\W+') AS Word, COUNT(1)
                FROM pg_catalog.pg_proc P
                JOIN pg_catalog.pg_namespace N ON N.oid = P.pronamespace
                JOIN pg_catalog.pg_language L ON L.oid = P.prolang
                WHERE L.lanname IN ('sql', 'plpgsql')
                AND N.nspname NOT IN ('pg_catalog', 'information_schema')
                GROUP BY Word
            ),
            OrderWords AS (
                SELECT Word,
                    ROW_NUMBER() OVER(PARTITION BY LOWER(Word) ORDER BY Count DESC)
                FROM Words
                WHERE Word ~* '.*[a-z].*'
            ),
            Names AS (
                --Column names
                SELECT attname AS Name
                FROM pg_catalog.pg_attribute
                UNION -- Table/view names
                SELECT relname
                FROM pg_catalog.pg_class
                UNION -- Function names
                SELECT proname
                FROM pg_catalog.pg_proc
                UNION -- Type names
                SELECT typname
                FROM pg_catalog.pg_type
                UNION -- Schema names
                SELECT nspname
                FROM pg_catalog.pg_namespace
                UNION -- Parameter names
                SELECT unnest(proargnames)
                FROM pg_proc
            )
            SELECT Word
            FROM OrderWords
            WHERE LOWER(Word) IN (SELECT Name FROM Names)
            AND Row_Number = 1;
            """
            _logger.debug("Casing Query. sql: %r", query)
            cur.execute(query)
            for row in cur:
                yield row[0]
