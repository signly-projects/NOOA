# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""LSP Skill module for NOOA agents."""

import pathlib
from typing import Dict

from nooa.skill import Skill

from .client import LSPClient
from .facade import LSPDocumentFacade
from .registry import LSPServerRegistry


class LSPSkill(Skill):
    """Provides Language Server Protocol (LSP) capabilities for code intelligence.

    ALL methods are async coroutines - always await them; calling without
    `await` returns a coroutine object, not a facade/result. Symbols returned
    by document_symbols() are dicts - use symbol["name"] and
    symbol["location"]["range"], not attribute access.

    Use this skill to perform repository-aware semantic code navigation:
        lsp = await self.lsp.for_file("src/orders.py")
        defs = await lsp.definition(line=10, character=5)
        refs = await lsp.references(line=10, character=5)

    For a name-based one-shot lookup (compiler-accurate alternative to text search):
        refs = await self.lsp.find_references("src/orders.py", "OrderService")
    """

    def __init__(self, root_uri: str | None = None):
        super().__init__()
        if root_uri is None:
            self._root_uri = pathlib.Path.cwd().as_uri()
        else:
            if not root_uri.startswith("file://"):
                self._root_uri = pathlib.Path(root_uri).absolute().as_uri()
            else:
                self._root_uri = root_uri

        self.registry = LSPServerRegistry()
        self._clients: Dict[tuple[str, ...], LSPClient] = {}
        self._opened_documents: set[str] = set()

    async def for_file(self, filepath: str) -> LSPDocumentFacade | None:
        """Get an LSP facade for a given file. Must be awaited.

        Args:
            filepath: Path to the source file (e.g., 'src/main.py').

        Returns:
            An LSPDocumentFacade, or None if no language server is available for the file extension.
        """
        path = pathlib.Path(filepath).absolute()
        ext = path.suffix

        server_config = self.registry.get_server_for_extension(ext)
        if not server_config:
            return None

        server_cmd_key = tuple(server_config.command)
        if server_cmd_key not in self._clients:
            client = LSPClient(command=server_config.command, root_uri=self._root_uri)
            await client.start()
            self._clients[server_cmd_key] = client

        client = self._clients[server_cmd_key]
        uri = path.as_uri()

        # Ensure the document is "open" from the LSP's perspective.
        if uri not in self._opened_documents:
            try:
                content = path.read_text(encoding="utf-8")
                lang_id = ext.lstrip(".")
                
                # Standardize common language IDs
                if lang_id == "py":
                    lang_id = "python"
                elif lang_id == "rs":
                    lang_id = "rust"
                elif lang_id == "js":
                    lang_id = "javascript"
                elif lang_id == "ts":
                    lang_id = "typescript"
                    
                await client.send_notification(
                    "textDocument/didOpen",
                    {
                        "textDocument": {
                            "uri": uri,
                            "languageId": lang_id,
                            "version": 1,
                            "text": content,
                        }
                    },
                )
                self._opened_documents.add(uri)
            except Exception:
                pass  # Ignore read errors

        return LSPDocumentFacade(client, uri)

    async def find_references(self, filepath: str, symbol: str) -> list:
        """Find all references to a named symbol in a file. Must be awaited.

        Compiler-accurate alternative to text-based search: resolves the
        symbol's declaration via document_symbols, then queries references
        at that exact position (declaration included in results).

        Args:
            filepath: Path to the file where the symbol is defined.
            symbol: The symbol name to find references for (e.g. 'get_llm_client').

        Returns:
            A list of LSP Locations, or an empty list if the symbol is not
            found or no language server is available for the file.

        Example:
            refs = await self.lsp.find_references("src/nooa/unifiedllm/registry.py", "get_llm_client")
        """
        facade = await self.for_file(filepath)
        if facade is None:
            return []

        symbols = await facade.document_symbols()
        if not symbols:
            return []

        for sym in symbols:
            if sym.get("name") != symbol:
                continue
            # SymbolInformation shape: location.range; DocumentSymbol shape: range / selectionRange.
            # Note: location.range spans the whole definition (its start may be the
            # `def`/`class` keyword at column 0), and some servers return no results
            # for a query on the keyword. Skip past leading keywords/whitespace so the
            # query position lands on the symbol name itself.
            loc = sym.get("location")
            rng = loc.get("range") if loc else sym.get("selectionRange") or sym.get("range")
            if not rng:
                continue
            start = rng.get("start", {})
            line, char = start.get("line", 0), start.get("character", 0)
            try:
                src_line = pathlib.Path(filepath).read_text(encoding="utf-8").splitlines()[line]
                idx = src_line.find(symbol, char)
                if idx >= 0:
                    char = idx
            except Exception:
                pass
            refs = await facade.references(line, char)
            return refs or []

        return []

    async def shutdown(self):
        """Shutdown all running LSP clients. Must be awaited."""
        for client in self._clients.values():
            await client.stop()
        self._clients.clear()
        self._opened_documents.clear()
