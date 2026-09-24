"""SMB share bootstrap for the download server.

Split out of download_server_io.py, which sits on the 700-line architecture
gate. The share is bound to download_token and declared read-only; impacket
is an optional dependency, so every entry point degrades to a warning when
it is missing. download_server_io.py re-exports these names, so importers
(download_server.py, tests/security) keep their existing import path.
"""

from __future__ import annotations

import threading

from core.log import logger


# ---------- SMB (impacket smbserver, optional dependency) ----------


def configure_smb_server(server, svc) -> None:
    """Bind the share to the download token and make it read-only (M14).

    http/sftp both authenticate with download_token; the SMB branch was the
    only channel accepting an anonymous session, so the token was worthless
    there as soon as download_server_host was not 127.0.0.1.
    """
    user, password = svc.smb_credentials()
    lmhash, nthash = _ntlm_hashes(password)
    # impacket's real signature is addCredential(name, uid, lmhash, nthash):
    # four required arguments taking NTLM hashes, never the plaintext token
    # (H4). Passing (user, password) raised TypeError inside the SMB thread
    # and the channel stayed silently dead.
    server.addCredential(user, 0, lmhash, nthash)
    # addShare feeds readOnly straight into ConfigParser.set() - a bool raises
    # TypeError - and every reader compares it against the literal "yes", so
    # a bool would never have been read-only even if accepted (H5).
    server.addShare(
        svc.smb_share(), svc._smb_dir.as_posix(), "cloud download share",
        readOnly="yes",
    )


def _ntlm_hashes(password: str) -> tuple[str, str]:
    """LM/NT hashes (hex) for impacket's addCredential() (H4)."""
    from impacket.ntlm import compute_lmhash, compute_nthash

    return compute_lmhash(password).hex(), compute_nthash(password).hex()


def start_smb_server(svc) -> None:
    """SMB share over the cache directory (share name "cloud").

    impacket is an optional dependency: without it the SMB channel is
    disabled and callers fall back to the http/sftp notice. Entries are
    materialized on demand (ensure_local / register_staged write into the
    shared directory before the user opens the UNC path).
    """
    if not svc.smb_available:
        logger.warning(
            "[dlserver] impacket not installed; smb disabled "
            "(pip install impacket)"
        )
        return

    def _serve() -> None:
        try:
            from impacket.smbserver import SimpleSMBServer

            server = SimpleSMBServer(
                listenAddress=svc.host, listenPort=svc.smb_port
            )
            configure_smb_server(server, svc)
            # Publish before start(): while the instance stayed a local,
            # shutdown()'s stop() was dead code, so the port remained bound
            # across plugin reloads and the cache root was deleted under a
            # share that was still being served (M12). impacket has no
            # setLogHim() - the default log_file='None' already means "no log
            # file", so no logging call is needed.
            svc._smb_server = server
            # Publish the listening socket so shutdown() can wake the accept
            # loop: impacket's stop() only calls server_close(), which on a
            # ThreadingMixIn/TCPServer is a no-op, so the bound port outlived
            # the service (L7).
            try:
                svc._smb_sock = server.getServer().socket
            except Exception:
                svc._smb_sock = None
            server.start()  # blocking
        except Exception as e:
            svc._smb_server = None
            svc._smb_sock = None
            # Bootstrap/config failure, not the "not installed" notice:
            # keep it visible instead of silently disabling the channel
            # (M12).
            logger.error(f"[dlserver] smb serve loop failed: {e}")

    svc._smb_thread = threading.Thread(target=_serve, daemon=True)
    svc._smb_thread.start()
    logger.info(
        f"[dlserver] smb share \\\\{svc.public_host}\\{svc.smb_share()} on :{svc.smb_port}"
    )


def stop_smb_server(svc) -> None:
    """Real teardown for the impacket SMB server (L7).

    ``SimpleSMBServer.stop()`` delegates to ``server_close()``, a no-op on
    ``socketserver.BaseServer``. The blocking ``serve_forever()`` loop exits
    only when ``shutdown()`` sets ``__shutdown_request``, so stop() neither
    ended the session nor released the port - the M12 symptom the SFTP
    branch had already fixed for itself.

    impacket's ``SMBSERVER`` subclasses ``socketserver.TCPServer``, so the
    public ``shutdown()``/``server_close()`` pair is available on the object
    returned by ``getServer()``. ``shutdown()`` blocks until the loop ends,
    which is exactly the ordering the cache-root removal below needs.
    """
    server = getattr(svc, "_smb_server", None)
    if server is None:
        return
    raw = None
    try:
        raw = server.getServer()
    except Exception:
        raw = None
    if raw is not None:
        try:
            raw.shutdown()  # sets __shutdown_request; joins serve_forever
        except Exception as e:
            logger.debug(f"[dlserver] smb serve_forever shutdown failed: {e}")
        try:
            raw.server_close()
        except Exception as e:
            logger.debug(f"[dlserver] smb server_close failed: {e}")
    else:
        # Fallback for a stub/older API without getServer(): best effort.
        try:
            server.stop()
        except Exception as e:
            logger.debug(f"[dlserver] smb stop failed: {e}")
    svc._smb_server = None
