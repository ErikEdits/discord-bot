"""Minecraft crash-log analyzer (pure functions, no Discord code).

analyze(text) takes the contents of a latest.log, a crash-reports/*.txt file,
a launcher log or a JVM hs_err_pid*.log and returns:

    {
        "info":      {"minecraft": "1.20.1", "loader": "Fabric", "loader_version": "0.15.3",
                      "java": "17.0.8", "mod_count": 152, "kind": "crash report", ...},
        "findings":  [Finding, ...]     # most likely cause first
        "root_error": "java.lang.NullPointerException: ..." | None,
        "stack_mods": ["sodium", "iris"],   # mods that show up in the stack trace
        "looks_like_crash": bool,
    }

Rules are plain regexes over the log text. To teach the analyzer a new error,
add a function to RULES that returns a list of Finding objects.
"""

import re
from collections import Counter
from dataclasses import dataclass

# Severity: 3 = almost certainly the cause, 2 = likely, 1 = hint / worth checking.


@dataclass
class Finding:
    title: str
    detail: str
    fix: str
    severity: int = 2


# ---------------------------------------------------------------------------
# Info extraction
# ---------------------------------------------------------------------------

_INFO_PATTERNS = [
    ("fabric", re.compile(r"Loading Minecraft (\S+) with Fabric Loader (\S+)")),
    ("quilt", re.compile(r"Loading Minecraft (\S+) with Quilt Loader (\S+)")),
]

_MC_VERSION = [
    re.compile(r"Minecraft Version: (\S+)"),
    re.compile(r"--fml\.mcVersion, ([\w.\-]+)"),
    re.compile(r"Minecraft Version ID: (\S+)"),
    re.compile(r"Loading Minecraft (\S+)"),
]
_NEOFORGE_VERSION = re.compile(r"--fml\.neoForgeVersion, ([\w.\-+]+)")
_FORGE_VERSION = re.compile(r"--fml\.forgeVersion, ([\w.\-+]+)")
_JAVA_VERSION = [
    re.compile(r"Java Version: ([\w.\-+]+)"),
    re.compile(r"java version ([\w.\-+]+)"),
    re.compile(r"JRE version: .*?\(([\w.\-+]+)"),
]
_MOD_COUNT = [
    re.compile(r"Loading (\d+) mods:"),
    re.compile(r"Found (\d+) mods? (?:to load|files)"),
]
_BRAND = re.compile(r"(?:Client brand changed to|Server brand changed to) '([^']+)'")
_DESCRIPTION = re.compile(r"^Description: (.+)$", re.MULTILINE)
_EXIT_CODE = re.compile(r"(?i)exit code:? (-?\d+)")


def _first(patterns, text):
    for p in patterns:
        m = p.search(text)
        if m:
            return m.group(1)
    return None


def extract_info(text: str) -> dict:
    info: dict = {}
    for loader, pattern in _INFO_PATTERNS:
        m = pattern.search(text)
        if m:
            info["minecraft"] = m.group(1)
            info["loader"] = loader.title()
            info["loader_version"] = m.group(2)
            break
    if "minecraft" not in info:
        mc = _first(_MC_VERSION, text)
        if mc:
            info["minecraft"] = mc.rstrip(",]")
    if "loader" not in info:
        neo = _NEOFORGE_VERSION.search(text)
        forge = _FORGE_VERSION.search(text)
        if neo:
            info["loader"], info["loader_version"] = "NeoForge", neo.group(1)
        elif forge:
            info["loader"], info["loader_version"] = "Forge", forge.group(1)
        else:
            brand = _BRAND.search(text)
            if brand and brand.group(1).lower() != "vanilla":
                info["loader"] = brand.group(1).title()
            elif "neoforge" in text.lower():
                info["loader"] = "NeoForge"
            elif "net.minecraftforge" in text or "fml.loading" in text:
                info["loader"] = "Forge"
            elif "net.fabricmc" in text:
                info["loader"] = "Fabric"
            elif "org.quiltmc" in text:
                info["loader"] = "Quilt"
    java = _first(_JAVA_VERSION, text)
    if java:
        info["java"] = java.rstrip(",")
    count = _first(_MOD_COUNT, text)
    if count:
        info["mod_count"] = int(count)
    desc = _DESCRIPTION.search(text)
    if desc:
        info["description"] = desc.group(1).strip()
    code = _EXIT_CODE.search(text)
    if code:
        info["exit_code"] = int(code.group(1))

    if "A fatal error has been detected by the Java Runtime Environment" in text:
        info["kind"] = "JVM crash log (hs_err)"
    elif "---- Minecraft Crash Report ----" in text:
        info["kind"] = "crash report"
    elif re.search(r"^\[\d\d:\d\d:\d\d\] \[", text, re.MULTILINE):
        info["kind"] = "game log"
    else:
        info["kind"] = "log"
    return info


# ---------------------------------------------------------------------------
# Root exception + mods in stack trace
# ---------------------------------------------------------------------------

_EXCEPTION_LINE = re.compile(
    r"^(?:Caused by: )?((?:[a-zA-Z_$][\w$]*\.)+[\w$]*(?:Exception|Error|Throwable)(?:: .*)?)$",
    re.MULTILINE,
)
_EXCEPTION_ANYWHERE = re.compile(r"((?:[a-z_$][\w$]*\.)+[\w$]*(?:Exception|Error)(?:: [^\n]*)?)")
_CAUSED_BY = re.compile(r"^Caused by: (.+)$", re.MULTILINE)

_STACK_MOD = re.compile(r"at (?:TRANSFORMER|SECURE-BOOTSTRAP|MC-BOOTSTRAP|LAYER PLUGIN|PLUGIN)/([\w\-]+)@")
_MIXIN_STACK = re.compile(r"\$(?:handler|redirect|modify|wrapOperation|localvar)\$[\w]*?\$([a-z][\w]*?)\$")
_IGNORED_STACK_MODS = {
    "minecraft", "forge", "neoforge", "fml", "fmlcore", "fmlloader", "javafmllanguage",
    "lowcodelanguage", "mclanguage", "eventbus", "modlauncher", "mixin", "bootstraplauncher",
    "securejarhandler", "coremods", "net.minecraftforge", "java.base", "jdk.internal",
}


def find_root_error(text: str) -> str | None:
    caused = _CAUSED_BY.findall(text)
    if caused:
        return caused[-1].strip()[:400]
    m = _EXCEPTION_LINE.search(text) or _EXCEPTION_ANYWHERE.search(text)
    return m.group(1).strip()[:400] if m else None


def find_stack_mods(text: str) -> list[str]:
    counts: Counter = Counter()
    for mod in _STACK_MOD.findall(text):
        if mod.lower() not in _IGNORED_STACK_MODS:
            counts[mod] += 1
    for mod in _MIXIN_STACK.findall(text):
        if mod.lower() not in _IGNORED_STACK_MODS:
            counts[mod] += 1
    return [m for m, _ in counts.most_common(5)]


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

def _dedupe(items):
    seen, out = set(), []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _bullets(items, limit=8):
    items = _dedupe(items)
    lines = [f"- {i}" for i in items[:limit]]
    if len(items) > limit:
        lines.append(f"- … and {len(items) - limit} more")
    return "\n".join(lines)


_FABRIC_DEP = re.compile(
    r"- Mod '(?P<mod>[^']+)' \((?P<id>[^)]+)\) (?P<ver>\S+) requires (?P<req>.+?) of "
    r"(?:mod )?(?P<dep>'[^']+' \([^)]+\)|[\w\-.]+), (?P<rest>which is missing|but only the wrong version is present: [^!\n]+)!"
)
_FABRIC_BREAKS = re.compile(
    r"- Mod '(?P<mod>[^']+)' \((?P<id>[^)]+)\) (?P<ver>\S+) is incompatible with (?P<req>.+?) of "
    r"(?:mod )?(?P<dep>'[^']+' \([^)]+\)|[\w\-.]+), but a matching version is present"
)
_FABRIC_SOLUTION = re.compile(
    r"A potential solution has been determined.*?:\s*\n(?P<body>(?:\s*- .+\n?)+)", re.IGNORECASE
)


def _pretty_dep(dep: str) -> str:
    m = re.match(r"'([^']+)' \(([^)]+)\)", dep)
    return f"{m.group(1)} ({m.group(2)})" if m else dep


def rule_fabric_dependencies(text):
    out = []
    missing, wrong, java = [], [], []
    for m in _FABRIC_DEP.finditer(text):
        dep = _pretty_dep(m.group("dep"))
        line = f"**{m.group('mod')}** needs {m.group('req')} of **{dep}**"
        if "(java)" in m.group("dep") or m.group("dep") == "java":
            present = m.group("rest").split(":")[-1].strip()
            java.append(f"{line} (you have Java {present})")
        elif m.group("rest").startswith("which is missing"):
            missing.append(line)
        else:
            present = m.group("rest").split(":")[-1].strip()
            wrong.append(f"{line} - installed: `{present}`")
    breaks = [
        f"**{m.group('mod')}** is incompatible with {m.group('req')} of **{_pretty_dep(m.group('dep'))}**"
        for m in _FABRIC_BREAKS.finditer(text)
    ]
    solution = _FABRIC_SOLUTION.search(text)
    solution_text = ""
    if solution:
        steps = [s.strip()[2:].strip() for s in solution.group("body").strip().splitlines() if s.strip().startswith("-")]
        if steps:
            solution_text = "Fabric suggests:\n" + _bullets(steps, 6)

    if missing:
        out.append(Finding(
            "Missing dependency",
            _bullets(missing),
            "Download the missing mod(s) for your Minecraft version and loader and put them in the mods folder.",
            3,
        ))
    if wrong:
        out.append(Finding(
            "Wrong version of a dependency",
            _bullets(wrong),
            "Update or downgrade the listed mod(s) so the versions match (check the Minecraft version too).",
            3,
        ))
    if java:
        out.append(Finding(
            "Wrong Java version",
            _bullets(java),
            "Install the required Java version (Minecraft 1.20.5+ needs Java 21, 1.18-1.20.4 needs Java 17) "
            "and select it in your launcher.",
            3,
        ))
    if breaks:
        out.append(Finding(
            "Incompatible mods",
            _bullets(breaks),
            "Remove one of the two mods or use versions that work together.",
            3,
        ))
    if out and solution_text:
        # Fabric's own suggestion is the most precise fix - show it once, on the first finding.
        out[0].fix = solution_text
    return out


_FORGE_MISSING = re.compile(
    r"Mod ID: '(?P<dep>[^']+)', Requested by: '(?P<by>[^']+)', Expected range: '(?P<range>[^']*)', Actual version: '(?P<actual>[^']*)'"
)
_NEO_REQUIRES = re.compile(
    r"Mod (?P<by>.+?) requires (?P<dep>.+?) (?P<range>\S+ or above|\S+ or below|\S+ to \S+|any version|\S+)\s*\n\s*Currently, (?P=dep) is (?P<state>not installed|[\w.\-+]+)"
)


def rule_forge_dependencies(text):
    missing, wrong = [], []
    for m in _FORGE_MISSING.finditer(text):
        line = f"**{m.group('by')}** needs **{m.group('dep')}** `{m.group('range')}`"
        if m.group("actual").upper() == "[MISSING]":
            missing.append(line)
        else:
            wrong.append(f"{line} - installed: `{m.group('actual')}`")
    for m in _NEO_REQUIRES.finditer(text):
        line = f"**{m.group('by')}** needs **{m.group('dep')}** {m.group('range')}"
        if m.group("state") == "not installed":
            missing.append(line)
        else:
            wrong.append(f"{line} - installed: `{m.group('state')}`")
    out = []
    if missing:
        out.append(Finding(
            "Missing dependency",
            _bullets(missing),
            "Download the missing mod(s) for your Minecraft version and loader and put them in the mods folder.",
            3,
        ))
    if wrong:
        out.append(Finding(
            "Wrong version of a dependency",
            _bullets(wrong),
            "Update or downgrade the listed mod(s) so the versions match. If it's `minecraft`, "
            "the mod is made for a different Minecraft version.",
            3,
        ))
    return out


_CLASS_VERSION = re.compile(
    r"compiled by a more recent version of the Java Runtime \(class file version (\d+)\.\d+\), "
    r"this version of the Java Runtime only recognizes class file versions up to (\d+)"
)
_MAJOR_TOO_NEW = re.compile(r"Unsupported class file major version (\d+)")


def rule_java_version(text):
    out = []
    m = _CLASS_VERSION.search(text)
    if m:
        need, have = int(m.group(1)) - 44, int(m.group(2)) - 44
        out.append(Finding(
            "Java is too old",
            f"A mod or the game needs **Java {need}**, but you're running **Java {have}**.",
            f"Install Java {need} (e.g. from adoptium.net) and select it in your launcher.",
            3,
        ))
    m = _MAJOR_TOO_NEW.search(text)
    if m:
        out.append(Finding(
            "Java is too new for this loader",
            f"The loader can't read Java {int(m.group(1)) - 44} class files.",
            "Use the Java version your Minecraft version expects (1.16: Java 8, 1.17-1.20.4: Java 17, "
            "1.20.5+: Java 21), or update the loader.",
            3,
        ))
    return out


_FORGE_DUPES = re.compile(r"Mod ID: '(?P<id>[^']+)' from mod files: (?P<files>.+)")
_DUPE_GENERIC = re.compile(
    r"(?i)(duplicate mods? found|found duplicate mods|DuplicateModsFoundException|duplicate mod[ :]+['\w]|is provided by multiple)"
)
_FABRIC_DUPE_ID = re.compile(r"(?i)duplicate mod(?:s)?:? '?([\w\-]+)'?")


def rule_duplicates(text):
    items = [f"`{m.group('id')}`: {m.group('files').strip()}" for m in _FORGE_DUPES.finditer(text)]
    if not items and _DUPE_GENERIC.search(text):
        items = [f"`{i}`" for i in _FABRIC_DUPE_ID.findall(text) if i.lower() not in {"found", "mod", "mods"}]
        if not items:
            items = ["(see the log for the exact files)"]
    if not items:
        return []
    return [Finding(
        "Duplicate mods",
        "The same mod is installed more than once:\n" + _bullets(items),
        "Keep only one (the newest) file of each mod in the mods folder.",
        3,
    )]


def rule_memory(text):
    out = []
    if "java.lang.OutOfMemoryError" in text:
        out.append(Finding(
            "Out of memory",
            "Minecraft ran out of RAM (`OutOfMemoryError`).",
            "Give the game more RAM in your launcher (usually 4-8 GB for modpacks, not more than half your PC's RAM).",
            3,
        ))
    if re.search(r"Could not reserve enough space for (?:\d+\w* )?object heap", text):
        out.append(Finding(
            "Java can't get the RAM you set",
            "Java couldn't reserve the configured memory.",
            "Lower the RAM setting in your launcher, and make sure you use 64-bit Java.",
            3,
        ))
    return out


_MIXIN_FAILED = re.compile(
    r"Mixin apply (?:for mod (?P<mod>[\w\-]+) )?failed (?P<cfg>[\w.\-]+\.json)?(?:.*?from mod (?P<from>[\w\-]+))?"
)
_MIXIN_CFG = re.compile(r"(?:Mixin|mixin)[^\n]{0,120}?([\w\-]+(?:\.[\w\-]+)*\.mixins?\.json|mixins\.[\w\-]+\.json)")


def _mod_from_mixin_config(cfg: str) -> str:
    name = cfg[:-5] if cfg.endswith(".json") else cfg
    parts = [p for p in name.split(".") if p not in ("mixins", "mixin", "json", "client", "common", "compat")]
    return parts[0] if parts else name


def rule_mixin(text):
    mods = []
    for m in _MIXIN_FAILED.finditer(text):
        mod = m.group("mod") or m.group("from") or (m.group("cfg") and _mod_from_mixin_config(m.group("cfg")))
        if mod:
            mods.append(mod)
    if not mods and ("MixinApplyError" in text or "MixinTransformerError" in text or "InvalidInjectionException" in text):
        mods = [_mod_from_mixin_config(c) for c in _MIXIN_CFG.findall(text)]
        if not mods:
            mods = ["(unknown - see the log)"]
    if not mods:
        return []
    mods = _dedupe(mods)
    return [Finding(
        "Mixin error",
        "A mod failed to modify the game code: " + ", ".join(f"**{m}**" for m in mods[:5]),
        "This mod is usually incompatible with another mod or made for a different Minecraft/loader version. "
        "Update it, or remove it to test.",
        3 if len(mods) <= 3 else 2,
    )]


_WRONG_LOADER = [
    re.compile(r"(?i)it is an? (forge|fabric|quilt|neoforge) mod"),
    re.compile(r"(?i)(?:is|are) (?:an? )?(forge|fabric|quilt) mods?,? (?:and|which) (?:cannot|can't|can not|will not) be loaded"),
    re.compile(r"(?i)(?:missing|without) (?:the )?(?:META-INF/)?(neoforge\.mods\.toml|mods\.toml)"),
]
_JAR_NAME = re.compile(r"([\w\-.+\[\]]+\.jar)")


def rule_wrong_loader(text):
    for pattern in _WRONG_LOADER:
        m = pattern.search(text)
        if m:
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_end = text.find("\n", m.end())
            line = text[line_start:line_end if line_end != -1 else None]
            jar = _JAR_NAME.search(line)
            what = f" (`{jar.group(1)}`)" if jar else ""
            return [Finding(
                "Mod for the wrong loader",
                f"A mod{what} is made for a different mod loader than the one you're using.",
                "Download the version of that mod for your loader (Fabric / Forge / NeoForge / Quilt) "
                "- Modrinth lets you filter by loader.",
                3,
            )]
    return []


_ACCESS_VIOLATION_DLL = re.compile(r"(?i)\b(atio6axx|atioglxx|amdxc64|nvoglv64|nvoglv32|ig\d+icd\d+|igxelpicd64|ig9icd64|igd10iumd64)\.dll")
_PROBLEMATIC_FRAME = re.compile(r"# Problematic frame:\s*\n#\s*\w\s+\[([^\]+]+)")


def rule_graphics(text):
    out = []
    gpu = None
    dll = _ACCESS_VIOLATION_DLL.search(text)
    if dll:
        name = dll.group(1).lower()
        gpu = "AMD" if name.startswith(("ati", "amd")) else "NVIDIA" if name.startswith("nv") else "Intel"
    frame = _PROBLEMATIC_FRAME.search(text)
    if gpu or re.search(r"GLFW error 65542|driver does not appear to support OpenGL|Pixel format not accelerated|Couldn't set pixel format", text):
        vendor = f" (**{gpu}**)" if gpu else ""
        out.append(Finding(
            "Graphics driver problem",
            f"The crash happened in the graphics driver{vendor} or OpenGL isn't available.",
            "Update your graphics driver from the vendor's website (not Windows Update). "
            "On laptops, make sure Java uses the dedicated GPU.",
            3,
        ))
    elif frame:
        out.append(Finding(
            "Native crash",
            f"Java itself crashed in `{frame.group(1).strip()}`.",
            "Update your graphics driver and Java. If it keeps happening, remove performance/rendering mods to test.",
            2,
        ))
    code = re.search(r"(?i)exit code:? (-1073741819|-1073740791|-805306369)", text)
    if code and not out:
        meaning = {
            "-1073741819": "an access violation (often the graphics driver)",
            "-1073740791": "a stack buffer overrun (often the graphics driver or an overlay)",
            "-805306369": "the game freezing and being killed (often RAM or a stuck mod)",
        }[code.group(1)]
        out.append(Finding(
            f"Exit code {code.group(1)}",
            f"This exit code usually means {meaning}.",
            "Update your graphics driver, close overlays (Discord, MSI Afterburner, ...) and check your RAM setting.",
            2,
        ))
    return out


_CONFIG_FAIL = [
    re.compile(r"Failed loading config file (?P<file>\S+) of type \w+ for modid (?P<mod>[\w\-]+)"),
    re.compile(r"(?:ParsingException|Failed to load config|Error loading config)[^\n]*?(?P<file>[\w\-./\\]+\.(?:toml|json5?|cfg|properties))"),
]


def rule_config(text):
    for pattern in _CONFIG_FAIL:
        m = pattern.search(text)
        if m:
            mod = m.groupdict().get("mod")
            who = f" of **{mod}**" if mod else ""
            return [Finding(
                "Broken config file",
                f"The config file `{m.group('file')}`{who} can't be read.",
                "Delete that file (the mod creates a fresh one on the next start) or fix the typo in it.",
                3,
            )]
    return []


_MISSING_CODE = re.compile(
    r"java\.lang\.(NoSuchMethodError|NoSuchFieldError|NoClassDefFoundError|ClassNotFoundException|AbstractMethodError)"
    r"(?::\s*'?(?:[\w$<>\[\]]+\s+)?([\w.$/]+))?"
)
_GENERIC_PACKAGE_PARTS = {
    "com", "net", "org", "io", "me", "dev", "de", "xyz", "gg", "uk", "fr", "it", "nl", "ru", "cn",
    "github", "mods", "mod", "client", "common", "impl", "api", "core", "main", "java", "lang",
}


def _guess_mod_from_class(name: str) -> str | None:
    name = name.replace("/", ".")
    if name.startswith(("net.minecraft.", "com.mojang.")):
        return "Minecraft"
    for part in name.split(".")[:-1]:
        if part.lower() not in _GENERIC_PACKAGE_PARTS and not part.startswith("class_"):
            return part
    return None


def rule_missing_code(text):
    m = _MISSING_CODE.search(text)
    if not m:
        return []
    kind, target = m.group(1), m.group(2) or ""
    guess = _guess_mod_from_class(target) if target else None
    if guess == "Minecraft":
        hint = "The code belongs to Minecraft itself, so a mod was made for a **different Minecraft version**."
    elif guess:
        hint = f"It looks like it belongs to **{guess}** - that mod is probably missing or the wrong version."
    else:
        hint = "A mod is probably missing, outdated or made for a different version."
    return [Finding(
        "Code not found",
        f"`{kind}`" + (f" for `{target[:120]}`" if target else "") + f".\n{hint}",
        "Make sure every mod matches your Minecraft version and loader, and that all required libraries are installed.",
        2,
    )]


def rule_server(text):
    out = []
    if "FAILED TO BIND TO PORT" in text:
        out.append(Finding(
            "Port already in use",
            "The server couldn't start because its port is already used.",
            "Close the other server instance (or change `server-port` in server.properties).",
            3,
        ))
    if "You need to agree to the EULA" in text:
        out.append(Finding(
            "EULA not accepted",
            "The server stops until the Minecraft EULA is accepted.",
            "Open `eula.txt` and set `eula=true`.",
            3,
        ))
    if re.search(r"A single server tick took [\d.]+ seconds|ServerHangWatchdog", text):
        out.append(Finding(
            "Server froze (watchdog)",
            "The server stopped responding for too long and was shut down.",
            "Look at the thread dump below 'Server Watchdog' for a mod name, or set `max-tick-time=-1` "
            "while testing. Often caused by world generation or a looping mod.",
            2,
        ))
    return out


def rule_optifine(text):
    if re.search(r"(?i)optifine", text) and re.search(r"(?i)\b(sodium|embeddium|iris|oculus|rubidium)\b", text):
        return [Finding(
            "OptiFine together with Sodium/Iris",
            "OptiFine and Sodium/Iris (or their Forge ports) don't work together.",
            "Remove OptiFine and keep Sodium/Iris (or Embeddium/Oculus on Forge).",
            3,
        )]
    if re.search(r"(?i)\boptifine\b", text) and re.search(r"(?i)fabric", text):
        return [Finding(
            "OptiFine on Fabric",
            "OptiFine (via OptiFabric) breaks many Fabric mods.",
            "Try without OptiFine - Sodium + Iris give the same features.",
            1,
        )]
    return []


def rule_session(text):
    if re.search(r"Invalid session|Failed to login: Invalid session|Failed to verify username", text):
        return [Finding(
            "Login/session problem",
            "Minecraft couldn't verify your account.",
            "Restart the launcher and log in again.",
            2,
        )]
    return []


RULES = [
    rule_fabric_dependencies,
    rule_forge_dependencies,
    rule_java_version,
    rule_duplicates,
    rule_memory,
    rule_wrong_loader,
    rule_mixin,
    rule_config,
    rule_graphics,
    rule_server,
    rule_optifine,
    rule_missing_code,
    rule_session,
]

_ERROR_HINT = re.compile(r"(?i)(exception|error|fatal|crash|failed)")


def analyze(text: str) -> dict:
    text = text.replace("\r\n", "\n")
    findings: list[Finding] = []
    for rule in RULES:
        try:
            findings.extend(rule(text))
        except Exception:  # a broken rule must never kill the whole analysis
            continue
    # Keep the first finding per title (rules are ordered by specificity).
    unique, seen = [], set()
    for f in findings:
        if f.title not in seen:
            seen.add(f.title)
            unique.append(f)
    unique.sort(key=lambda f: -f.severity)
    return {
        "info": extract_info(text),
        "findings": unique,
        "root_error": find_root_error(text),
        "stack_mods": find_stack_mods(text),
        "looks_like_crash": bool(_ERROR_HINT.search(text)),
    }
