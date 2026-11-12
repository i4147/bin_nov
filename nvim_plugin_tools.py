#!/data/data/com.termux/files/usr/bin/env python
"""Neovim plugin helper: folderize, lock and split.
The ``folderize`` subcommand moves the ``.lua`` files into per-plugin folders by the references they contain, ``lock`` writes a ``lazy-lock.json`` from the installed lazy.nvim plugins (with the git commit of each) and ``split`` breaks a plugin-spec table into one file per plugin; ``--dry-run`` previews.
Usage: nvim_plugin_toolbox {folderize,lock,split} [options] Positional arguments: input Input .lua file, or '-' for stdin (default: '-').
Subcommands: folderize Move .lua files into per-plugin folders based on reference regexes.
lock Generate lazy-lock.json from installed lazy.nvim plugins.
split Split a Lua plugin-spec table into one file per plugin.
Options: --dry-run Show the plan without moving files.
--lazy-dir Directory containing installed plugin git repos (default: ~/.local/share/nvim/lazy).
--output Path of the lazy-lock.json file to write (default: ~/.config/nvim/lazy-lock.json).
-o, --output Output directory.
Defaults: '.' for --engine strict, 'plugins' for --engine simple.
--engine Strict = split_lua_plugins.py behavior (validation + rename map); simple = split_plugins.py behavior (no validation, sanitized names).
--validate Validate Lua syntax of generated files (default: strict).
--no-validate Skip Lua syntax validation.
--rename-map Apply built-in short-name rename map (default: strict).
--no-rename-map Do not apply the rename map.
--sanitize Sanitize file names (default: simple).
...
plus 4 more; run ``nvim_plugin_toolbox --help`` for the full list.
Examples: $ nvim_plugin_toolbox lock --output lazy-lock.json $ nvim_plugin_toolbox split plugins.lua --engine strict Command-line script: run ``python nvim_plugin_toolbox.py`` or the ``nvim_plugin_toolbox`` command.
The module is importable as well."""

import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Iterable, Optional, Sequence

PLUGIN_PATTERNS: dict[str, list[str]] = {
    "lazy.nvim": [
        r"lazy\.setup",
        r"lazy\.nvim",
        r'require\(["\']lazy["\']',
        r"\blazy\b",
    ],
    "packer.nvim": [
        r"packer\.setup",
        r"packer\.nvim",
        r'require\(["\']packer["\']',
        r"\bpacker\b",
    ],
    "vim-plug": [r"vim-plug", r"plug#begin", r'\bPlug\s+["\']'],
    "telescope": [r"telescope", r'require\(["\']telescope["\']', r"telescope\.setup"],
    "fzf-lua": [r"fzf-lua", r"fzf_lua", r'require\(["\']fzf-lua["\']'],
    "treesitter": [
        r"treesitter",
        r"nvim-treesitter",
        r"tree-sitter",
        r'require\(["\']nvim-treesitter["\']',
    ],
    "lualine": [r"lualine", r'require\(["\']lualine["\']', r"lualine\.setup"],
    "bufferline": [r"bufferline", r"buffer-line", r'require\(["\']bufferline["\']'],
    "statuscol": [r"statuscol", r"status-column", r'require\(["\']statuscol["\']'],
    "indent-blankline": [
        r"indent-blankline",
        r"indent_blankline",
        r"ibl\.setup",
        r'require\(["\']ibl["\']',
    ],
    "mini.nvim": [r"mini\.", r'require\(["\']mini\.'],
    "noice": [r"noice", r'require\(["\']noice["\']', r"noice\.setup"],
    "notify": [r"nvim-notify", r"notify\.setup", r'require\(["\']notify["\']'],
    "dressing": [r"dressing", r"dressing\.setup", r'require\(["\']dressing["\']'],
    "dashboard": [
        r"dashboard-nvim",
        r"dashboard\.setup",
        r'require\(["\']dashboard["\']',
    ],
    "alpha": [r"alpha-nvim", r"alpha\.setup", r'require\(["\']alpha["\']'],
    "which-key": [
        r"which-key",
        r"which_key",
        r"whichkey",
        r'require\(["\']which-key["\']',
    ],
    "legendary": [r"legendary", r"legendary\.setup", r'require\(["\']legendary["\']'],
    "nvim-cmp": [
        r"nvim-cmp",
        r"nvim_cmp",
        r"cmp\.setup",
        r'require\(["\']cmp["\']',
        r'require\(["\']cmp_nvim',
    ],
    "lspconfig": [r"lspconfig", r"nvim-lspconfig", r'require\(["\']lspconfig["\']'],
    "mason": [
        r"mason",
        r"mason-nvim",
        r"mason-lspconfig",
        r"mason\.setup",
        r'require\(["\']mason["\']',
    ],
    "mason-lspconfig": [
        r"mason-lspconfig",
        r"mason_lspconfig",
        r'require\(["\']mason-lspconfig["\']',
    ],
    "mason-tool-installer": [
        r"mason-tool-installer",
        r"mason_tool_installer",
        r'require\(["\']mason-tool-installer["\']',
    ],
    "null-ls": [r"null-ls", r"null_ls", r'require\(["\']null-ls["\']'],
    "none-ls": [r"none-ls", r"none_ls", r'require\(["\']none-ls["\']'],
    "conform": [r"conform", r"conform\.setup", r'require\(["\']conform["\']'],
    "efm-langserver": [r"efm-langserver", r"efm\.setup", r'require\(["\']efm["\']'],
    "fidget": [r"fidget", r"fidget\.setup", r'require\(["\']fidget["\']'],
    "lsp-status": [r"lsp-status", r"lsp_status", r'require\(["\']lsp-status["\']'],
    "lsp-signature": [
        r"lsp-signature",
        r"lsp_signature",
        r'require\(["\']lsp_signature["\']',
    ],
    "lsp-lines": [r"lsp-lines", r"lsp_lines", r'require\(["\']lsp_lines["\']'],
    "goto-preview": [
        r"goto-preview",
        r"goto_preview",
        r'require\(["\']goto-preview["\']',
    ],
    "lspsaga": [r"lspsaga", r"lspsaga\.setup", r'require\(["\']lspsaga["\']'],
    "lsp-ui": [r"lsp-ui", r"lsp_ui", r'require\(["\']lspconfig["\'].*lsp-ui'],
    "luasnip": [r"luasnip", r"lua-snip", r'require\(["\']luasnip["\']', r"ls\.setup"],
    "snippy": [r"snippy", r"snippy\.setup", r'require\(["\']snippy["\']'],
    "ultisnips": [r"ultisnips", r"UltiSnips", r"ultisnips#"],
    "friendly-snippets": [
        r"friendly-snippets",
        r"friendly_snippets",
        r'require\(["\']friendly-snippets["\']',
    ],
    "gitsigns": [r"gitsigns", r"gitsigns\.setup", r'require\(["\']gitsigns["\']'],
    "neogit": [r"neogit", r"neogit\.setup", r'require\(["\']neogit["\']'],
    "vim-fugitive": [r"vim-fugitive", r"fugitive", r":Git\b"],
    "git-blame": [r"git-blame", r"git_blame", r"gitblame"],
    "gitlinker": [r"gitlinker", r"git-linker", r'require\(["\']gitlinker["\']'],
    "diffview": [r"diffview", r"diffview\.setup", r'require\(["\']diffview["\']'],
    "octo": [r"octo\.nvim", r"octo\.setup", r'require\(["\']octo["\']'],
    "git-conflict": [r"git-conflict", r"git_conflict", r"git-conflict\.setup"],
    "neo-tree": [r"neo-tree", r"neo_tree", r"neotree", r'require\(["\']neo-tree["\']'],
    "nvim-tree": [
        r"nvim-tree",
        r"nvim_tree",
        r'require\(["\']nvim-tree["\']',
        r"nvim-tree\.setup",
    ],
    "oil": [r"oil\.setup", r"oil\.nvim", r'require\(["\']oil["\']', r"\boil\b"],
    "chad-tree": [r"chad-tree", r"chad_tree", r'require\(["\']nvchad'],
    "harpoon": [
        r"harpoon",
        r"harpoon2",
        r'require\(["\']harpoon["\']',
        r"harpoon\.setup",
    ],
    "hop": [r"hop\.setup", r"hop\.nvim", r'require\(["\']hop["\']'],
    "leap": [r"leap\.setup", r"leap\.nvim", r'require\(["\']leap["\']'],
    "flash": [r"flash\.setup", r"flash\.nvim", r'require\(["\']flash["\']'],
    "easymotion": [r"easymotion", r"easy-motion", r"vim-easymotion"],
    "marks": [r"marks\.setup", r"marks\.nvim", r'require\(["\']marks["\']'],
    "grapple": [r"grapple", r"grapple\.setup", r'require\(["\']grapple["\']'],
    "arrow": [r"arrow\.setup", r"arrow\.nvim", r'require\(["\']arrow["\']'],
    "surround": [
        r"nvim-surround",
        r"surround\.setup",
        r'require\(["\']nvim-surround["\']',
    ],
    "autopairs": [
        r"nvim-autopairs",
        r"autopairs\.setup",
        r'require\(["\']nvim-autopairs["\']',
    ],
    "comment": [
        r"comment\.setup",
        r"nvim-comment",
        r"Comment\.setup",
        r'require\(["\']Comment["\']',
    ],
    "ts-comments": [r"ts-comments", r"ts_comments", r'require\(["\']ts-comments["\']'],
    "tcomment": [r"tcomment", r"t-comment", r"vim-tcomment"],
    "vim-commentary": [r"vim-commentary", r"commentary"],
    "dial": [r"dial\.setup", r"dial\.nvim", r'require\(["\']dial["\']'],
    "substitute": [
        r"substitute\.setup",
        r"substitute\.nvim",
        r'require\(["\']substitute["\']',
    ],
    "ultimate-autopair": [r"ultimate-autopair", r"ultimate_autopair"],
    "vim-visual-multi": [r"vim-visual-multi", r"visual-multi", r"visual_multi"],
    "vim-illuminate": [r"illuminate", r"vim-illuminate", r"illuminate\.setup"],
    "todo-comments": [
        r"todo-comments",
        r"todo_comments",
        r"todocomments",
        r'require\(["\']todo-comments["\']',
    ],
    "twilight": [r"twilight\.setup", r"twilight\.nvim", r'require\(["\']twilight["\']'],
    "zen-mode": [
        r"zen-mode",
        r"zen_mode",
        r'require\(["\']zen-mode["\']',
        r"zen-mode\.setup",
    ],
    "true-zen": [r"true-zen", r"true_zen", r'require\(["\']true-zen["\']'],
    "colorizer": [r"colorizer", r"nvim-colorizer", r"colorizer\.setup"],
    "highlight-colors": [
        r"highlight-colors",
        r"highlight_colors",
        r"highlight-colors\.setup",
    ],
    "vim-hexokinase": [r"hexokinase", r"vim-hexokinase"],
    "dap": [r"nvim-dap", r"dap\.setup", r'require\(["\']dap["\']', r"\bdap\b"],
    "dap-ui": [r"dap-ui", r"dapui", r"dap_ui", r'require\(["\']dapui["\']'],
    "dap-python": [r"dap-python", r"dap_python", r'require\(["\']dap-python["\']'],
    "dap-go": [r"dap-go", r"dap_go", r'require\(["\']dap-go["\']'],
    "nvim-dap-virtual-text": [
        r"dap-virtual-text",
        r"dap_virtual_text",
        r"nvim-dap-virtual-text",
    ],
    "neotest": [r"neotest", r"neotest\.setup", r'require\(["\']neotest["\']'],
    "vim-test": [r"vim-test", r"vim_test", r"vim-test#"],
    "plenary": [r"plenary", r'require\(["\']plenary["\']'],
    "toggleterm": [
        r"toggleterm",
        r"toggle-term",
        r"toggleterm\.setup",
        r'require\(["\']toggleterm["\']',
    ],
    "floaterm": [r"floaterm", r"float-term", r"floaterm#"],
    "FTerm": [r"FTerm\.setup", r"ft-nvim", r'require\(["\']FTerm["\']'],
    "auto-session": [r"auto-session", r"auto_session", r"auto-session\.setup"],
    "persistence": [
        r"persistence\.setup",
        r"persistence\.nvim",
        r'require\(["\']persistence["\']',
    ],
    "project": [
        r"project\.nvim",
        r"project\.setup",
        r'require\(["\']project_nvim["\']',
    ],
    "telescope-project": [
        r"telescope-project",
        r"telescope_project",
        r"telescope._extensions.project",
    ],
    "trouble": [r"trouble", r"trouble\.setup", r'require\(["\']trouble["\']'],
    "spectre": [r"spectre", r"spectre\.setup", r'require\(["\']spectre["\']'],
    "nvim-bqf": [r"bnf", r"nvim-bqf", r"bqf\.setup"],
    "vim-ripgrep": [r"vim-ripgrep", r"Ripgrep", r"ripgrep#"],
    "tagbar": [r"tagbar", r"tag-bar", r"tagbar#"],
    "vista": [r"vista", r"vista\.setup", r"vista#"],
    "symbols-outline": [
        r"symbols-outline",
        r"symbols_outline",
        r"symbols-outline\.setup",
    ],
    "aerial": [r"aerial\.setup", r"aerial\.nvim", r'require\(["\']aerial["\']'],
    "vim-dadbod": [r"vim-dadbod", r"dadbod", r"dadbod#"],
    "dadbod-ui": [r"dadbod-ui", r"dadbod_ui", r"dadbod-ui\.setup"],
    "sqlite": [r"sqlite\.lua", r"sqlite", r'require\(["\']sqlite["\']'],
    "vim-go": [r"vim-go", r"vim_go", r"\bgo#"],
    "rust-tools": [r"rust-tools", r"rust_tools", r"rust-tools\.setup"],
    "rustaceanvim": [
        r"rustaceanvim",
        r"rustacean\.setup",
        r'require\(["\']rustaceanvim["\']',
    ],
    "vim-python": [r"vim-python", r"python-mode", r"python-syntax"],
    "vim-javascript": [r"vim-javascript", r"javascript\.vim", r"vim-js"],
    "typescript-tools": [
        r"typescript-tools",
        r"typescript\.tools",
        r"typescript-tools\.setup",
    ],
    "vim-vue": [r"vim-vue", r"vim_vue", r"vue\.vim"],
    "vim-react": [r"vim-react", r"vim_react", r"vim-jsx"],
    "vim-markdown": [r"vim-markdown", r"markdown\.vim", r"vim_markdown"],
    "markdown-preview": [
        r"markdown-preview",
        r"markdown_preview",
        r"markdown-preview\.setup",
    ],
    "vim-tex": [r"vim-tex", r"vimtex", r"latex"],
    "vim-julia": [r"vim-julia", r"julia-vim", r"julia\.vim"],
    "vim-r": [r"vim-r", r"vim_r", r"Nvim-R"],
    "vim-scala": [r"vim-scala", r"scala-vim", r"scala\.vim"],
    "undo-tree": [r"undo-tree", r"undo_tree", r"undotree", r"undo-tree\.setup"],
    "whichkey": [r"whichkey", r"which-key", r"which_key"],
    "vim-repeat": [r"vim-repeat", r"vim_repeat", r"repeat\.vim"],
    "vim-surround": [r"vim-surround", r"vim_surround", r"surround\.vim"],
    "vim-unimpaired": [r"vim-unimpaired", r"unimpaired", r"unimpaired\.vim"],
    "vim-abolish": [r"vim-abolish", r"abolish", r"abolish\.vim"],
    "vim-speeddating": [r"vim-speeddating", r"speeddating", r"speeddating\.vim"],
    "vim-exchange": [r"vim-exchange", r"exchange", r"exchange\.vim"],
    "vim-characterize": [r"vim-characterize", r"characterize", r"characterize\.vim"],
    "vim-textobj": [r"vim-textobj", r"textobj", r"textobj-"],
    "nvim-treesitter-textobjects": [
        r"treesitter-textobjects",
        r"textobjects\.setup",
        r'require\(["\']nvim-treesitter-textobjects["\']',
    ],
    "splitjoin": [r"splitjoin", r"split-join", r"splitjoin\.vim"],
    "vim-sort": [r"vim-sort", r"sort\.vim", r"\bsort#"],
    "vim-easy-align": [
        r"vim-easy-align",
        r"easy-align",
        r"easy_align",
        r"easy-align\.setup",
    ],
    "tabular": [r"tabular", r"tabular#", r"tabular\.vim"],
    "vim-argwrap": [r"vim-argwrap", r"argwrap", r"argwrap\.vim"],
    "prettier": [r"prettier", r"prettier\.setup", r"vim-prettier", r"prettier-nvim"],
    "eslint": [r"eslint", r"eslint\.setup", r"vim-eslint", r"eslint-nvim"],
    "stylelint": [r"stylelint", r"stylelint\.setup", r"vim-stylelint"],
    "ale": [r"\bale\b", r"ale\.vim", r"ale#", r"vim-ale"],
    "vim-lint": [r"vim-lint", r"vim_lint", r"lint\.vim"],
    "nvim-web-devicons": [
        r"nvim-web-devicons",
        r"web-devicons",
        r"devicons\.setup",
        r'require\(["\']nvim-web-devicons["\']',
    ],
    "lspkind": [
        r"lspkind",
        r"lsp-kind",
        r"lspkind\.setup",
        r'require\(["\']lspkind["\']',
    ],
    "vim-devicons": [r"vim-devicons", r"vim_devicons", r"devicons"],
    "nerd-fonts": [r"nerd-fonts", r"nerd_fonts", r"nerdfonts"],
    "tokyonight": [
        r"tokyonight",
        r"tokyo-night",
        r"tokyonight\.setup",
        r"tokyonight\.load",
    ],
    "catppuccin": [r"catppuccin", r"catppuccin\.setup", r"catppuccin\.load"],
    "onedark": [r"onedark", r"one-dark", r"onedark\.setup", r"onedark\.load"],
    "gruvbox": [r"gruvbox", r"gruvbox\.setup", r"gruvbox\.load"],
    "nord": [r"nord", r"nord\.setup", r"nord\.load"],
    "rose-pine": [r"rose-pine", r"rose_pine", r"rose-pine\.setup", r"rose-pine\.load"],
    "kanagawa": [r"kanagawa", r"kanagawa\.setup", r"kanagawa\.load"],
    "github-theme": [r"github-theme", r"github_theme", r"github-theme\.setup"],
    "dracula": [r"dracula", r"dracula\.setup", r"dracula\.load"],
    "everforest": [r"everforest", r"ever-forest", r"everforest\.setup"],
    "material": [r"material\.setup", r"material\.load", r"material-theme"],
    "nightfox": [r"nightfox", r"night-fox", r"nightfox\.setup"],
    "ayu": [r"ayu", r"ayu-vim", r"ayu\.setup"],
    "solarized": [r"solarized", r"solarized\.setup", r"solarized\.load"],
    "melange": [r"melange", r"melange\.setup", r"melange\.load"],
    "vim-colorschemes": [r"vim-colorschemes", r"colorschemes", r"vim_colorschemes"],
    "impatient": [r"impatient", r"impatient\.setup", r"impatient\.nvim"],
    "vim-startuptime": [r"vim-startuptime", r"startuptime", r"vim_startuptime"],
    "profile": [r"profile\.nvim", r"profile\.setup", r'require\(["\']profile["\']'],
    "vim-which-key": [r"vim-which-key", r"vim_which_key", r"vim-whichkey"],
    "keys": [r"keys\.setup", r"keys\.nvim", r'require\(["\']keys["\']'],
    "cheatsheet": [r"cheatsheet", r"cheat-sheet", r"cheat\.setup"],
    "vim-help": [r"vim-help", r"help\.vim", r"vim_help"],
    "vim-tmux": [r"vim-tmux", r"tmux\.vim", r"vim_tmux", r"tmux-navigator"],
    "tmux-navigator": [r"tmux-navigator", r"tmux_navigator", r"tmux-navigator\.setup"],
    "vim-tmux-navigator": [r"vim-tmux-navigator", r"vim_tmux_navigator"],
    "window-picker": [r"window-picker", r"window_picker", r"window-picker\.setup"],
    "winshift": [r"winshift", r"win-shift", r"winshift\.setup"],
    "vim-maximizer": [r"vim-maximizer", r"vim_maximizer", r"maximizer\.vim"],
    "focus": [r"focus\.nvim", r"focus\.setup", r'require\(["\']focus["\']'],
    "scrollbar": [r"scrollbar", r"scroll-bar", r"scrollbar\.setup"],
    "nvim-scrollview": [r"nvim-scrollview", r"scrollview", r"scrollview\.setup"],
    "smoothscroll": [r"smoothscroll", r"smooth-scroll", r"smooth-scroll\.setup"],
    "neoscroll": [r"neoscroll", r"neo-scroll", r"neoscroll\.setup"],
    "vim-smoothie": [r"vim-smoothie", r"smoothie", r"smoothie\.vim"],
    "vim-remote": [r"vim-remote", r"remote\.vim", r"vim_remote"],
    "netrw": [r"netrw", r"netrw\.vim", r"netrw#"],
    "vim-ssh": [r"vim-ssh", r"ssh\.vim", r"vim_ssh"],
    "vim-scp": [r"vim-scp", r"scp\.vim", r"vim_scp"],
    "vim-obsession": [r"vim-obsession", r"obsession", r"obsession\.vim"],
    "vim-startify": [r"vim-startify", r"startify", r"startify#"],
    "vim-projectionist": [r"vim-projectionist", r"projectionist", r"projectionist#"],
    "vim-dispatch": [r"vim-dispatch", r"dispatch\.vim", r"dispatch#"],
    "vim-eunuch": [r"vim-eunuch", r"eunuch", r"eunuch\.vim"],
    "vim-rails": [r"vim-rails", r"rails\.vim", r"vim_rails"],
    "vim-ruby": [r"vim-ruby", r"ruby\.vim", r"vim_ruby"],
    "vim-elixir": [r"vim-elixir", r"elixir\.vim", r"vim_elixir"],
    "vim-clojure": [r"vim-clojure", r"clojure\.vim", r"vim_clojure"],
    "vim-haskell": [r"vim-haskell", r"haskell\.vim", r"vim_haskell"],
    "vim-lua": [r"vim-lua", r"lua\.vim", r"vim_lua"],
    "vim-rust": [r"vim-rust", r"rust\.vim", r"vim_rust"],
    "vim-crystal": [r"vim-crystal", r"crystal\.vim", r"vim_crystal"],
    "vim-nim": [r"vim-nim", r"nim\.vim", r"vim_nim"],
    "vim-zig": [r"vim-zig", r"zig\.vim", r"vim_zig"],
    "vim-dart": [r"vim-dart", r"dart\.vim", r"vim_dart"],
    "vim-flutter": [r"vim-flutter", r"flutter\.vim", r"vim_flutter"],
    "vim-kotlin": [r"vim-kotlin", r"kotlin\.vim", r"vim_kotlin"],
    "vim-swift": [r"vim-swift", r"swift\.vim", r"vim_swift"],
    "vim-solidity": [r"vim-solidity", r"solidity\.vim", r"vim_solidity"],
    "vim-php": [r"vim-php", r"php\.vim", r"vim_php"],
    "vim-perl": [r"vim-perl", r"perl\.vim", r"vim_perl"],
    "vim-raku": [r"vim-raku", r"raku\.vim", r"vim_raku"],
}
RENAME_MAP: dict[str, str] = {
    "nvim-lspconfig": "lsp",
    "nvim-treesitter": "treesitter",
    "nvim-cmp": "cmp",
    "nvim-lint": "lint",
    "nvim-dap": "dap",
    "nvim-dap-ui": "dap-ui",
    "nvim-dap-virtual-text": "dap-virtual-text",
    "nvim-dap-python": "dap-python",
    "nvim-surround": "surround",
    "nvim-autopairs": "autopairs",
    "nvim-colorizer": "colorizer",
    "nvim-notify": "notify",
    "nvim-bqf": "bqf",
    "nvim-illuminate": "illuminate",
}


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    n = 1
    while True:
        candidate = parent / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def find_plugin_refs(path: Path, patterns: dict[str, list[str]] = PLUGIN_PATTERNS) -> set[str]:
    found: set[str] = set()
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Error reading {path}: {exc}", file=sys.stderr)
        return found
    fname = path.name.lower()
    for name, pats in patterns.items():
        for pat in pats:
            if re.search(pat, content, re.IGNORECASE):
                found.add(name)
                break
        for pat in pats:
            if re.search(pat, fname, re.IGNORECASE):
                found.add(name)
                break
    return found


def _safe_move(src: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists() and dest != src:
        stem, suffix = src.stem, src.suffix
        n = 1
        while dest.exists():
            dest = dest_dir / f"{stem}_{n}{suffix}"
            n += 1
        print(f"  ⚠️  Name conflict: {src.name} → {dest.name}")
    shutil.move(str(src), str(dest))
    return dest


def cmd_folderize(args: argparse.Namespace) -> int:
    root = Path.cwd()
    lua_files = list(root.rglob("*.lua"))
    if not lua_files:
        print("No .lua files found in the current directory tree.")
        return 0
    print(f"Found {len(lua_files)} .lua files")
    print("Scanning for plugin references...\n")
    by_plugin: dict[str, list[Path]] = defaultdict(list)
    unclassified: list[Path] = []
    for path in lua_files:
        refs = find_plugin_refs(path)
        rel = path.relative_to(root)
        if refs:
            for ref in refs:
                by_plugin[ref].append(path)
            print(f"  {rel}: {','.join(sorted(refs))}")
        else:
            unclassified.append(path)
            print(f"  {rel}: No plugins detected")
    print("\n" + "=" * 40)
    print("Organization Plan:")
    print("=" * 40)
    for name in sorted(by_plugin):
        files = by_plugin[name]
        print(f"\n📁 {name}/ ({len(files)} files)")
        for p in files:
            print(f"  → {p.relative_to(root)}")
    if unclassified:
        print(f"\n📁 unclassified/ ({len(unclassified)} files)")
        for p in unclassified:
            print(f"  → {p.relative_to(root)}")
    if args.dry_run:
        print("\n[DRY RUN] No files were moved. Run without --dry-run to organize files.")
        return 0
    answer = input("\nProceed with moving files? (y/N): ").strip().lower()
    if answer not in ("y", "yes"):
        print("Operation cancelled.")
        return 0
    print("\nMoving files...")
    moved = 0
    for name, files in by_plugin.items():
        target_dir = root / name
        for src in files:
            try:
                dest = _safe_move(src, target_dir)
                moved += 1
                print(f"  ✓ {src.relative_to(root)} → {dest.relative_to(root)}")
            except Exception as exc:
                print(f"  ✗ Failed to move {src}: {exc}")
    if unclassified:
        target_dir = root / "unclassified"
        for src in unclassified:
            try:
                dest = _safe_move(src, target_dir)
                moved += 1
                print(f"  ✓ {src.relative_to(root)} → {dest.relative_to(root)}")
            except Exception as exc:
                print(f"  ✗ Failed to move {src}: {exc}")
    print(f"\n✅ Completed! Moved {moved} files.")
    return 0


def _git_info(repo: Path) -> tuple[Optional[str], Optional[str]]:
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return commit, branch
    except subprocess.CalledProcessError:
        return None, None


def cmd_lock_main(args: argparse.Namespace) -> int:
    lazy_dir = Path(args.lazy_dir).expanduser()
    output = Path(args.output).expanduser()
    print("Generating lazy-lock.json for Neovim plugins...")
    print(f"Scanning: {lazy_dir}")
    print(f"Output:   {output}")
    print("-" * 40)
    if not lazy_dir.exists():
        print(f"Error: Lazy directory not found at {lazy_dir}")
        print("\nFailed to generate lock file.")
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)
    lock_data: dict[str, dict[str, str]] = {}
    for entry in sorted(lazy_dir.iterdir()):
        if not entry.is_dir():
            continue
        name = entry.name
        if not (entry / ".git").exists():
            print(f"Skipping {name}: Not a git repository")
            continue
        commit, branch = _git_info(entry)
        if commit and branch:
            lock_data[name] = {"branch": branch, "commit": commit}
            print(f"✓ {name}: {commit[:8]} ({branch})")
        else:
            print(f"✗ {name}: Failed to get git information")
    try:
        with open(output, "w", encoding="utf-8") as fh:
            json.dump(lock_data, fh, indent=2)
            fh.write("\n")
    except OSError as exc:
        print(f"Error writing lock file: {exc}")
        print("\nFailed to generate lock file.")
        return 1
    print(f"\n✓ Successfully wrote lock file to {output}")
    print(f"  Total plugins: {len(lock_data)}")
    print("\nDone! You can now use this lock file with lazy.nvim.")
    return 0


def _find_block(text: str, start: int) -> Optional[tuple[int, int]]:
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_str = False
    quote: Optional[str] = None
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch in ("'", '"'):
            if not in_str:
                in_str, quote = True, ch
            elif ch == quote:
                in_str, quote = False, None
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return (start, i + 1)
    return None


def _split_top_level(body: str) -> list[str]:
    entries: list[str] = []
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch in " \t\n\r,":
            i += 1
            continue
        if ch == "{":
            span = _find_block(body, i)
            if span:
                start, end = span
                entries.append(body[start:end].strip())
                i = end
                continue
        j = i
        depth = 0
        in_str = False
        quote: Optional[str] = None
        escaped = False
        while j < n:
            c = body[j]
            if escaped:
                escaped = False
                j += 1
                continue
            if c == "\\":
                escaped = True
                j += 1
                continue
            if c in ("'", '"'):
                if not in_str:
                    in_str, quote = True, c
                elif c == quote:
                    in_str, quote = False, None
                j += 1
                continue
            if in_str:
                j += 1
                continue
            if c in "({[":
                depth += 1
            elif c in ")}]":
                depth -= 1
            elif c == "," and depth == 0:
                break
            j += 1
        i = j + 1 if j < n else n
    return entries


_URL_PATTERNS: tuple[str, str, str, str] = (
    r'"([^"]+/[^"]+)"',
    r"'([^']+/[^']+)'",
    r'\[\s*"([^"]+/[^"]+)"\s*\]',
    r"\[\s*'([^']+/[^']+)'\s*\]",
)


def _extract_plugin_url(block: str) -> Optional[str]:
    for pat in _URL_PATTERNS:
        m = re.search(pat, block)
        if m:
            return m.group(1)
    m = re.search(r'["\']([a-zA-Z0-9_-]+/[a-zA-Z0-9._-]+)["\']', block)
    return m.group(1) if m else None


def _format_plugin_block(block: str) -> str:
    lines = [ln.strip() for ln in block.strip().split("\n")]
    body = "\n".join(lines).strip()
    if body.startswith("return"):
        return body
    if body.startswith("{"):
        return f"return {body}"
    return f"return {{\n  {body}\n}}"


def _balanced_braces(code: str) -> bool:
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack: list[str] = []
    in_str = False
    quote: Optional[str] = None
    escaped = False
    for ch in code:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch in ("'", '"'):
            if not in_str:
                in_str, quote = True, ch
            elif ch == quote:
                in_str, quote = False, None
            continue
        if in_str:
            continue
        if ch in pairs:
            stack.append(ch)
        elif ch in pairs.values() and (not stack or pairs[stack.pop()] != ch):
            return False
    return not stack and not in_str


def _lua_syntax_valid(code: str) -> bool:
    try:
        proc = subprocess.run(
            ["luac", "-p", "-"],
            input=code.encode(),
            capture_output=True,
            timeout=5,
            check=False,
        )
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    try:
        wrapped = f"return (function() {code} end)()"
        proc = subprocess.run(
            ["lua", "-e", wrapped],
            capture_output=True,
            timeout=5,
            check=False,
        )
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return _balanced_braces(code)


def cmd_split(args: argparse.Namespace) -> int:
    strict = args.engine == "strict"
    do_validate = args.validate if args.validate is not None else strict
    use_rename_map = args.rename_map if args.rename_map is not None else strict
    sanitize_names = args.sanitize if args.sanitize is not None else (not strict)
    input_path: Optional[Path]
    if args.input == "-":
        if sys.stdin.isatty():
            print("Error: no input file given (stdin is a TTY)", file=sys.stderr)
            return 1
        source = sys.stdin.read()
        input_path = None
    else:
        input_path = Path(args.input)
        if not input_path.exists():
            print(f"Error: File not found: {input_path}", file=sys.stderr)
            return 1
        source = input_path.read_text(encoding="utf-8")
    if args.output is not None:
        out_dir = Path(args.output)
    elif strict:
        out_dir = Path()
    else:
        out_dir = Path("plugins")
    out_dir.mkdir(parents=True, exist_ok=True)
    ret_idx = source.find("return")
    if ret_idx == -1:
        ret_idx = 0
    brace_idx = source.find("{", ret_idx)
    if brace_idx == -1:
        print("Error: No table found in file", file=sys.stderr)
        return 1
    span = _find_block(source, brace_idx)
    if not span:
        print("Error: Unbalanced braces in file", file=sys.stderr)
        return 1
    _, end = span
    body = source[brace_idx + 1 : end - 1]
    specs = _split_top_level(body)
    created: list[Path] = []
    total = len(specs)
    for idx, spec in enumerate(specs, 1):
        if not spec.strip() or spec.strip() == "{}":
            continue
        url = _extract_plugin_url(spec)
        if not url:
            print(
                f"Warning: Could not parse plugin name from block, skipping:\n{spec[:100]}...",
                file=sys.stderr,
            )
            continue
        short = url.split("/")[-1].removesuffix(".nvim")
        if use_rename_map:
            short = RENAME_MAP.get(short, short)
        if sanitize_names:
            short = re.sub(r"[^a-zA-Z0-9\-_.]", "_", short)
        content = _format_plugin_block(spec)
        if do_validate and not _lua_syntax_valid(content):
            print(f"Error: Invalid Lua syntax for {url}, skipping", file=sys.stderr)
            print(f"Content:\n{content[:200]}...", file=sys.stderr)
            continue
        out_path = out_dir / f"{short}.lua"
        if strict:
            out_path = _unique_path(out_path)
        out_path.write_text(content, encoding="utf-8")
        created.append(out_path)
        print(f"[{idx}/{total}] Created: {out_path} <- {url}")
    if input_path is not None:
        if args.move:
            backup = _unique_path(input_path.with_suffix(input_path.suffix + ".bak"))
            input_path.rename(backup)
            input_path.write_text("return {\n}\n", encoding="utf-8")
            print(f"Replaced {input_path} with empty table (backup: {backup})")
        elif args.delete_input:
            input_path.unlink()
            print(f"{input_path} removed.")
        elif args.keep_input:
            pass
        elif strict:
            pass
        else:
            input_path.unlink()
            print(f"{input_path} removed.")
    print(f"\nTotal files created: {len(created)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nvim_plugin_toolbox.py",
        description="Unified Neovim plugin helper (folderize / lock / split).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_folder = sub.add_parser(
        "folderize",
        help="Move .lua files into per-plugin folders based on reference regexes.",
    )
    p_folder.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the plan without moving files.",
    )
    p_folder.set_defaults(func=cmd_folderize)
    p_lock = sub.add_parser(
        "lock",
        help="Generate lazy-lock.json from installed lazy.nvim plugins.",
    )
    p_lock.add_argument(
        "--lazy-dir",
        default=str(Path.home() / ".local" / "share" / "nvim" / "lazy"),
        help="Directory containing installed plugin git repos (default: ~/.local/share/nvim/lazy).",
    )
    p_lock.add_argument(
        "--output",
        default=str(Path.home() / ".config" / "nvim" / "lazy-lock.json"),
        help="Path of the lazy-lock.json file to write (default: ~/.config/nvim/lazy-lock.json).",
    )
    p_lock.set_defaults(func=cmd_lock_main)
    p_split = sub.add_parser(
        "split",
        help="Split a Lua plugin-spec table into one file per plugin.",
    )
    p_split.add_argument(
        "input",
        nargs="?",
        default="-",
        help="Input .lua file, or '-' for stdin (default: '-').",
    )
    p_split.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output directory. Defaults: '.' for --engine strict, 'plugins' for --engine simple.",
    )
    p_split.add_argument(
        "--engine",
        choices=["strict", "simple"],
        default="strict",
        help="strict = split_lua_plugins.py behavior (validation + rename map); "
        "simple = split_plugins.py behavior (no validation, sanitized names).",
    )
    p_split.add_argument(
        "--validate",
        dest="validate",
        action="store_true",
        default=None,
        help="Validate Lua syntax of generated files (default: strict).",
    )
    p_split.add_argument(
        "--no-validate",
        dest="validate",
        action="store_false",
        help="Skip Lua syntax validation.",
    )
    p_split.add_argument(
        "--rename-map",
        dest="rename_map",
        action="store_true",
        default=None,
        help="Apply built-in short-name rename map (default: strict).",
    )
    p_split.add_argument(
        "--no-rename-map",
        dest="rename_map",
        action="store_false",
        help="Do not apply the rename map.",
    )
    p_split.add_argument(
        "--sanitize",
        dest="sanitize",
        action="store_true",
        default=None,
        help="Sanitize file names (default: simple).",
    )
    p_split.add_argument(
        "--no-sanitize",
        dest="sanitize",
        action="store_false",
        help="Do not sanitize file names.",
    )
    p_split.add_argument(
        "-m",
        "--move",
        action="store_true",
        help="Backup the input file and replace it with an empty table (`return {\\n}`).",
    )
    p_split.add_argument(
        "--keep-input",
        action="store_true",
        help="Do not delete or modify the input file.",
    )
    p_split.add_argument(
        "--delete-input",
        action="store_true",
        help="Delete the input file after splitting (default for --engine simple).",
    )
    p_split.set_defaults(func=cmd_split)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    rc = args.func(args)
    return int(rc) if rc is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
