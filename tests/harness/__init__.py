"""sumtag conformance harness.

A standalone end-to-end test harness for sumtag. It builds real files in
precisely-known states (``corpus``), runs the *actual* ``sumtag`` command
against them, then inspects what really happened on disk (``oracle``) and
reports pass/fail (``runner`` / ``report``).

It is deliberately **independent of sumtag's own code** so it can act as an
oracle: it has its own xattr access (``xattrio``) and its own copy of the
schema/hash logic (``schema``). A bug shared between writer and reader cannot
hide if the reader never shares code with the writer. See the testing-strategy
note and CLAUDE.md.

Run it with::

    python -m tests.harness
"""
