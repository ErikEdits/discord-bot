"""Minecraft crash-log analyzer (pure functions, no Discord code).

analyze(text) takes the contents of a latest.log, a crash-reports/*.txt file,
a launcher log or a JVM hs_err_pid*.log and returns:

    {
        "info":      {"minecraft": "1.20.1", "loader": "Fabric", "loader_version": "0.15.3",
                      "java": "17.0.8", "mod_count": 152, "kind": "crash report", ...},
        "findings":  [Finding, ...]     # most likely cause first
        "root_error": "java.lang.NullPointerException: ..." | None,
        "stack_mods": ["sodium", "iris"],   # mods that show up in the stack trace
        "suspects":  [{"mod": "Sodium", "likelihood": "very likely", "why": "..."}],
        "mods":      {"sodium": "0.5.8+mc1.20.1", ...},   # installed mods, if the log lists them
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
    key: str = ""   # what exactly (e.g. "missing:fabric-api") - used to spot problems many people have


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
            elif "net.fabricmc" in text or "Fabric Mods:" in text:
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
    missing_ids, wrong_ids = [], []
    for m in _FABRIC_DEP.finditer(text):
        dep = _pretty_dep(m.group("dep"))
        dep_id = (re.search(r"\(([^)]+)\)", m.group("dep")) or re.match(r"(.*)", m.group("dep"))).group(1)
        line = f"**{m.group('mod')}** needs {m.group('req')} of **{dep}**"
        if "(java)" in m.group("dep") or m.group("dep") == "java":
            present = m.group("rest").split(":")[-1].strip()
            java.append(f"{line} (you have Java {present})")
        elif m.group("rest").startswith("which is missing"):
            missing.append(line)
            missing_ids.append(dep_id)
        else:
            present = m.group("rest").split(":")[-1].strip()
            wrong.append(f"{line} - installed: `{present}`")
            wrong_ids.append(dep_id)
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
            3, "missing:" + ",".join(sorted(set(missing_ids))),
        ))
    if wrong:
        out.append(Finding(
            "Wrong version of a dependency",
            _bullets(wrong),
            "Update or downgrade the listed mod(s) so the versions match (check the Minecraft version too).",
            3, "version:" + ",".join(sorted(set(wrong_ids))),
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


_FABRIC_OLD_MISSING = re.compile(r"Could not find required mod: (?P<by>[\w\-]+) requires \{(?P<dep>[\w\-]+) @ \[(?P<range>[^\]]*)\]\}")


def rule_forge_dependencies(text):
    missing, wrong, missing_ids, wrong_ids = [], [], [], []
    for m in _FORGE_MISSING.finditer(text):
        line = f"**{m.group('by')}** needs **{m.group('dep')}** `{m.group('range')}`"
        if m.group("actual").upper() == "[MISSING]":
            missing.append(line)
            missing_ids.append(m.group("dep"))
        else:
            wrong.append(f"{line} - installed: `{m.group('actual')}`")
            wrong_ids.append(m.group("dep"))
    for m in _NEO_REQUIRES.finditer(text):
        line = f"**{m.group('by')}** needs **{m.group('dep')}** {m.group('range')}"
        if m.group("state") == "not installed":
            missing.append(line)
            missing_ids.append(m.group("dep"))
        else:
            wrong.append(f"{line} - installed: `{m.group('state')}`")
            wrong_ids.append(m.group("dep"))
    for m in _FABRIC_OLD_MISSING.finditer(text):
        missing.append(f"**{m.group('by')}** needs **{m.group('dep')}** `{m.group('range')}`")
        missing_ids.append(m.group("dep"))
    out = []
    if missing:
        out.append(Finding(
            "Missing dependency",
            _bullets(missing),
            "Download the missing mod(s) for your Minecraft version and loader and put them in the mods folder.",
            3, "missing:" + ",".join(sorted(set(missing_ids))),
        ))
    if wrong:
        out.append(Finding(
            "Wrong version of a dependency",
            _bullets(wrong),
            "Update or downgrade the listed mod(s) so the versions match. If it's `minecraft`, "
            "the mod is made for a different Minecraft version.",
            3, "version:" + ",".join(sorted(set(wrong_ids))),
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
        3 if len(mods) <= 3 else 2, "mixin:" + mods[0],
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
                3, "loader:" + (jar.group(1) if jar else ""),
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
                3, "config:" + (mod or m.group("file")),
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
    hit = known_package(name)
    if hit:
        return hit[0]
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


# ---------------------------------------------------------------------------
# Known packages -> mod names (libraries people often forget, popular mods)
# ---------------------------------------------------------------------------

KNOWN_PACKAGES = [
    # libraries
    ("net.fabricmc.fabric.", "Fabric API", True),
    ("dev.architectury.", "Architectury API", True),
    ("me.shedaniel.architectury.", "Architectury API", True),
    ("me.shedaniel.clothconfig2.", "Cloth Config API", True),
    ("me.shedaniel.autoconfig.", "Cloth Config API", True),
    ("me.shedaniel.rei.", "Roughly Enough Items", False),
    ("software.bernie.geckolib", "GeckoLib", True),
    ("mod.azure.azurelib.", "AzureLib", True),
    ("dev.isxander.yacl", "YetAnotherConfigLib (YACL)", True),
    ("com.terraformersmc.modmenu.", "Mod Menu", False),
    ("top.theillusivec4.curios.", "Curios API", True),
    ("dev.emi.trinkets.", "Trinkets", True),
    ("dev.emi.emi.", "EMI", False),
    ("mezz.jei.", "JEI", False),
    ("fuzs.puzzleslib.", "Puzzles Lib", True),
    ("fuzs.forgeconfigapiport.", "Forge Config API Port", True),
    ("net.minecraftforge.fml.config.", "Forge Config API Port", True),
    ("net.blay09.mods.balm.", "Balm", True),
    ("dev.ftb.mods.ftblibrary.", "FTB Library", True),
    ("vazkii.patchouli.", "Patchouli", True),
    ("com.teamresourceful.resourcefullib.", "Resourceful Lib", True),
    ("com.teamresourceful.resourcefulconfig.", "Resourceful Config", True),
    ("io.github.fabricators_of_create.porting_lib.", "Porting Lib", True),
    ("dev.kosmx.playeranim.", "playerAnimator", True),
    ("com.jamieswhiteshirt.reachentityattributes.", "Reach Entity Attributes", True),
    ("team.lodestar.lodestone.", "Lodestone", True),
    ("net.tslat.smartbrainlib.", "SmartBrainLib", True),
    ("com.llamalad7.mixinextras.", "MixinExtras", True),
    ("snownee.kiwi.", "Kiwi", True),
    ("com.supermartijn642.core.", "SuperMartijn642's Core Lib", True),
    ("com.supermartijn642.configlib.", "SuperMartijn642's Config Lib", True),
    ("io.wispforest.owo.", "oωo-lib", True),
    ("dev.onyxstudios.cca.", "Cardinal Components API", True),
    ("org.ladysnake.cca.", "Cardinal Components API", True),
    ("com.github.alexthe666.citadel.", "Citadel", True),
    ("net.darkhax.bookshelf.", "Bookshelf", True),
    ("com.teamabnormals.blueprint.", "Blueprint", True),
    ("dev.latvian.mods.rhino.", "Rhino", True),
    ("dev.latvian.mods.kubejs.", "KubeJS", False),
    ("terrablender.", "TerraBlender", True),
    ("net.fabricmc.language.kotlin.", "Fabric Language Kotlin", True),
    ("kotlin.", "Kotlin library (Fabric Language Kotlin / Kotlin for Forge)", True),
    ("thedarkcolour.kotlinforforge.", "Kotlin for Forge", True),
    ("com.mrcrayfish.framework.", "Framework", True),
    ("dev.toma.configuration.", "Configuration", True),
    ("me.fzzyhmstrs.fzzy_config.", "Fzzy Config", True),
    ("net.createmod.catnip.", "Create (Catnip)", True),
    ("com.tterrag.registrate.", "Registrate (part of Create)", True),
    ("org.sinytra.connector.", "Sinytra Connector", False),
    ("dev.su5ed.sinytra.", "Sinytra Connector", False),
    ("org.sinytra.fabric.", "Forgified Fabric API", True),
    # popular mods (for naming the culprit)
    ("me.jellysquid.mods.sodium.", "Sodium", False),
    ("net.caffeinemc.mods.sodium.", "Sodium", False),
    ("me.jellysquid.mods.lithium.", "Lithium", False),
    ("net.caffeinemc.mods.lithium.", "Lithium", False),
    ("net.coderbot.iris.", "Iris", False),
    ("net.irisshaders.", "Iris", False),
    ("org.embeddedt.embeddium.", "Embeddium", False),
    ("me.jellysquid.mods.phosphor.", "Phosphor", False),
    ("ca.spottedleaf.starlight.", "Starlight", False),
    ("com.simibubi.create.", "Create", False),
    ("net.optifine.", "OptiFine", False),
    ("optifine.", "OptiFine", False),
    ("de.erikedits.", "ErikEdits mod", False),
    ("journeymap.", "JourneyMap", False),
    ("xaero.", "Xaero's Minimap / World Map", False),
    ("com.github.alexthe666.iceandfire.", "Ice and Fire", False),
    ("net.mehvahdjukaar.moonlight.", "Moonlight Lib", True),
    ("net.mehvahdjukaar.", "{next}", False),
    ("com.yungnickyoung.", "YUNG's API", True),
    ("dev.tr7zw.", "tr7zw mod (EntityCulling / NotEnoughAnimations / ...)", False),
    ("com.natamus.collective.", "Collective", True),
    ("com.natamus.", "{next}", False),
    ("traben.entity_texture_features.", "Entity Texture Features", False),
    ("traben.entity_model_features.", "Entity Model Features", False),
    ("dev.lambdaurora.lambdynlights.", "LambDynamicLights", False),
    ("com.mojang.blaze3d.", "Minecraft (rendering)", False),
]

# Stack frames from these never count as the culprit.
_FRAMEWORK_PREFIXES = (
    "java.", "javax.", "jdk.", "sun.", "com.sun.", "net.minecraft.", "com.mojang.", "net.fabricmc.loader.",
    "net.fabricmc.api.", "org.quiltmc.loader.", "net.minecraftforge.fml.", "net.minecraftforge.eventbus.",
    "net.minecraftforge.common.", "net.minecraftforge.client.", "net.minecraftforge.event.",
    "net.minecraftforge.registries.", "net.neoforged.fml.", "net.neoforged.bus.", "net.neoforged.neoforge.",
    "cpw.mods.", "org.spongepowered.", "org.objectweb.", "com.google.", "org.apache.", "io.netty.",
    "it.unimi.", "org.lwjgl.", "oshi.", "ca.weblite.", "com.llamalad7.mixinextras.", "org.slf4j.",
    "kotlin.", "kotlinx.", "org.jetbrains.", "com.electronwill.", "org.openjdk.", "net.minecraftforge.network.",
    "org.sinytra.connector.", "dev.su5ed.sinytra.", "net.fabricmc.fabric.impl.base.",
)


def known_package(class_name: str) -> tuple[str, bool] | None:
    """(mod name, is a library) for a class name like dev.architectury.event.Event."""
    name = class_name.replace("/", ".").lstrip("L")
    for prefix, mod, library in KNOWN_PACKAGES:
        if name.startswith(prefix):
            if mod == "{next}":   # e.g. com.natamus.<modname>.
                mod = name[len(prefix):].split(".")[0]
            return mod, library
    return None


# ---------------------------------------------------------------------------
# More rules
# ---------------------------------------------------------------------------

_MISSING_CLASS = re.compile(r"(?:NoClassDefFoundError|ClassNotFoundException):\s*'?L?([\w.$/]+)")


def rule_missing_library(text):
    libs = {}
    for cls in _MISSING_CLASS.findall(text):
        hit = known_package(cls)
        if hit and hit[1] and not cls.replace("/", ".").startswith("net.minecraftforge.fml.config."):
            libs.setdefault(hit[0], cls.replace("/", "."))
        elif cls.replace("/", ".").startswith("net.minecraftforge.fml.config.") and "net.fabricmc" in text:
            libs.setdefault("Forge Config API Port", cls.replace("/", "."))
    if not libs:
        return []
    names = list(libs)
    return [Finding(
        "Missing library mod",
        "These are needed by another mod but aren't installed (or are the wrong version):\n"
        + _bullets([f"**{n}** (`{libs[n][:80]}`)" for n in names]),
        "Download them from Modrinth/CurseForge for your Minecraft version and loader.",
        3, "library:" + ",".join(sorted(names)),
    )]


_TICKING_SECTION = re.compile(
    r"-- (?P<what>Entity being ticked|Block entity being ticked|Block being got|Block being placed|"
    r"Tesselating block model|Block model being tesselated|Item being rendered|Entity being rendered|"
    r"Feature placement|Entity being loaded) --\s*\n(?P<body>(?:.*\n){1,12})"
)
_NAMESPACED = re.compile(r"(?:Entity Type|Name|Block|Item Type|Feature|Type|Block Entity Type)"
                         r": (?:Block\{|ResourceKey\[[\w:]+ / )?(?P<ns>[a-z0-9_.\-]+):(?P<id>[a-z0-9_/.\-]+)")


def rule_ticking(text):
    m = _TICKING_SECTION.search(text)
    if not m:
        return []
    what = m.group("what").lower()
    nm = _NAMESPACED.search(m.group("body"))
    if not nm:
        return []
    ns, thing = nm.group("ns"), f"{nm.group('ns')}:{nm.group('id')}"
    kind = ("entity" if "entity being" in what and "block" not in what else
            "block entity" if "block entity" in what else
            "item" if "item" in what else "world generation" if "feature" in what else "block")
    a = "an" if kind[0] in "aeiou" else "a"
    if ns == "minecraft":
        return [Finding(
            f"Crash at {a} {kind}",
            f"The game crashed while handling the vanilla {kind} `{thing}`. A mod that changes it is probably "
            "the cause (see the suspects).",
            "Update the mods that change this, or test without them. If the world won't load anymore, restore "
            "a backup of the world.",
            2, f"tick:{thing}",
        )]
    return [Finding(
        f"Crash caused by {a} {kind} from {ns}",
        f"The game crashed while handling `{thing}` - that belongs to the mod **{ns}**.",
        f"Update **{ns}** (or remove it to test). If the world won't load anymore, restore a backup or "
        f"remove that {kind} with a world editor (e.g. MCA Selector).",
        3, f"tick:{ns}",
    )]


_FORGE_FAILED_MOD = re.compile(
    r"(?P<name>[\w .'+\-]{2,60}?) \((?P<id>[a-z0-9_\-]{2,64})\) (?:has failed to load correctly|"
    r"encountered an error during the (?P<phase>[\w ]+?) event phase)"
)


def rule_mod_failed(text):
    mods = _dedupe(f"{m.group('name').strip()} (`{m.group('id')}`)" for m in _FORGE_FAILED_MOD.finditer(text))
    if not mods:
        return []
    first = _FORGE_FAILED_MOD.search(text).group("id")
    return [Finding(
        "Mod failed to load",
        "These mods crashed while loading:\n" + _bullets(mods),
        "Update them (and their required libraries) or remove them to test. Check that they're made for your "
        "Minecraft and loader version.",
        3, f"failed:{first}",
    )]


_WRONG_SIDE = [
    re.compile(r"Attempted to load class (?P<cls>\S+) for invalid dist DEDICATED_SERVER"),
    re.compile(r"Environment type CLIENT is not supported"),
    re.compile(r"(?:NoClassDefFoundError|ClassNotFoundException): (?P<cls>net/minecraft/client/\S+)"),
]


def rule_wrong_side(text):
    if "dedicated" not in text.lower() and "server" not in text.lower():
        return []
    for p in _WRONG_SIDE:
        m = p.search(text)
        if m and ("DEDICATED_SERVER" in text or "Environment type CLIENT" in text or "MinecraftServer" in text):
            return [Finding(
                "Client-only mod on a server",
                "A mod that only works in the game client (graphics, menus, minimaps ...) is installed on the server.",
                "Remove client-only mods from the server's mods folder (e.g. Sodium, Iris, minimaps, Mod Menu, "
                "sound/visual mods). The suspects below show which one.",
                3, "client-on-server",
            )]
    return []


_BROKEN_JAR = re.compile(r"(?i)(zip END header not found|ZipException|error in opening zip file|Invalid or corrupt "
                         r"jarfile|Failed to read mod file|invalid LOC header|java\.util\.zip\.\w+Exception)")


def rule_broken_jar(text):
    m = _BROKEN_JAR.search(text)
    if not m:
        return []
    line = text[text.rfind("\n", 0, m.start()) + 1:text.find("\n", m.end()) if text.find("\n", m.end()) != -1 else None]
    jar = _JAR_NAME.search(line) or _JAR_NAME.search(text[m.end():m.end() + 300])
    what = f" (`{jar.group(1)}`)" if jar else ""
    return [Finding(
        "Damaged mod file",
        f"A mod file{what} is broken or only half downloaded.",
        "Delete it and download it again (from Modrinth or CurseForge, not from re-upload sites).",
        3, "jar:" + (jar.group(1) if jar else ""),
    )]


def rule_shader(text):
    if re.search(r"(?i)(failed to compile (?:shader|program)|shader compilation failed|ShaderCompileException|"
                 r"error while (?:loading|compiling) shader ?pack|Couldn't compile shader|IrisPipeline.*Exception)", text):
        return [Finding(
            "Shader pack problem",
            "A shader pack couldn't be compiled.",
            "Update the shader pack and Iris/Oculus, try another shader pack, or turn shaders off. "
            "Also update your graphics driver.",
            2, "shader",
        )]
    return []


def rule_world(text):
    if re.search(r"(?i)(Couldn't load chunk|Failed to (?:load|read) chunk|Exception reading [^\n]*region|"
                 r"Failed to read level\.dat|corrupted chunk|Couldn't read chunk|Error loading region file|"
                 r"Chunk file at \[[^\]]+\] is in the wrong location)", text):
        return [Finding(
            "Damaged world / chunks",
            "Parts of the world (chunks or level.dat) can't be read.",
            "Restore a backup of the world. Single broken chunks can be deleted with MCA Selector. "
            "Always close the game normally so the world can be saved.",
            2, "world",
        )]
    return []


def rule_language_provider(text):
    m = re.search(r"needs language provider (\S+?)(?::|\s)", text)
    if m:
        return [Finding(
            "Mod for a different loader version",
            f"A mod needs the language provider `{m.group(1)}`, which this loader doesn't have. "
            "It's made for an older/newer Forge or for Forge instead of NeoForge (or the other way round).",
            "Download the version of the mod for exactly your loader and Minecraft version.",
            3, "language:" + m.group(1),
        )]
    return []


def rule_vram(text):
    if re.search(r"GL_OUT_OF_MEMORY|(?i:out of video memory)", text):
        return [Finding(
            "Out of video memory",
            "The graphics card ran out of memory.",
            "Lower render distance, texture pack resolution and shader settings, or use a smaller modpack.",
            2, "vram",
        )]
    return []


def rule_connector(text):
    if re.search(r"org\.sinytra\.connector|dev\.su5ed\.sinytra", text):
        return [Finding(
            "Fabric mods running through Sinytra Connector",
            "Some Fabric mods are loaded on (Neo)Forge with Connector. That often breaks.",
            "Test without the Fabric mods (or without Connector) to see if the crash goes away.",
            1, "connector",
        )]
    return []


def rule_illegal_access(text):
    m = re.search(r"java\.lang\.IllegalAccessError: (?:class|tried to access)[^\n]{0,200}", text)
    if m:
        return [Finding(
            "Mod made for a different version",
            f"`{m.group(0)[:200]}`",
            "A mod uses parts of Minecraft/the loader that changed. Update the mods and the loader to versions "
            "made for each other.",
            2, "illegal-access",
        )]
    return []


# ---------------------------------------------------------------------------
# Installed mods + the most likely culprit
# ---------------------------------------------------------------------------

_FABRIC_MOD_LINE = re.compile(r"^\s*- ([a-z0-9_\-]{2,64}) (\S+)\s*$", re.MULTILINE)
_FABRIC_REPORT_MOD = re.compile(r"^\t\t([a-z0-9_\-]{2,64}): (.+?) (\S+)\s*$", re.MULTILINE)
_FORGE_REPORT_MOD = re.compile(r"^\s*\S+\.jar\s*\|(?P<name>[^|]+)\|(?P<id>[a-z0-9_\-]+)\s*\|(?P<ver>[^|\s]+)\s*\|",
                               re.MULTILINE)
_LOADING_MODS = re.compile(r"Loading \d+ mods:\n((?:\s+(?:- |\|-- ).*\n?)+)")


def extract_mods(text: str) -> dict[str, str]:
    """modid -> version of the installed mods, when the log lists them."""
    mods: dict[str, str] = {}
    block = _LOADING_MODS.search(text)
    if block:
        for mod_id, ver in _FABRIC_MOD_LINE.findall(block.group(1)):
            mods.setdefault(mod_id, ver)
    for mod_id, _name, ver in _FABRIC_REPORT_MOD.findall(text):
        mods.setdefault(mod_id, ver)
    for m in _FORGE_REPORT_MOD.finditer(text):
        mods.setdefault(m.group("id"), m.group("ver"))
    return mods


_FRAME = re.compile(r"^\s*at (?:[\w\-]+(?:@[\w.\-+]+)?/)?([\w$.]+)\.[\w$<>]+\([^)]*\)(?:\s*[~?]?\[([^\]]*)\])?",
                    re.MULTILINE)
_JAR_IN_FRAME = re.compile(r"([\w\-+.]+?)(?:[-_+](?:mc|fabric|forge|neoforge|quilt)?[\d][\w.+\-]*)?\.jar")


def _stack_after_root(text: str) -> str:
    """The stack trace of the deepest 'Caused by' that has frames, else the first exception's."""
    starts = [m.start() for m in re.finditer(r"\nCaused by: ", text)]
    for idx in reversed(starts):
        part = text[idx:idx + 8000]
        nxt = part.find("\nCaused by: ", 1)
        if _FRAME.search(part[:nxt] if nxt != -1 else part):
            return part
    m = _EXCEPTION_LINE.search(text)
    idx = m.start() if m else 0
    return text[idx:idx + 8000]


_ENTRYPOINT = re.compile(r"Could not execute entrypoint stage '\w+' due to errors, provided by '([\w\-]+)'")


def _frame_mod(cls: str, bracket: str | None, mod_ids: set[str] = frozenset()) -> str | None:
    hit = known_package(cls)
    if hit and not hit[0].startswith("Minecraft"):
        return hit[0]
    if cls.startswith(_FRAMEWORK_PREFIXES):
        return None
    for part in cls.split(".")[:-1]:   # a package named like an installed mod
        if part.lower() in mod_ids:
            return part
    if bracket:
        jar = _JAR_IN_FRAME.search(bracket)
        if jar and not re.match(r"(?i)(minecraft|client|server|forge|neoforge|fmlloader|fabric-loader|"
                                r"intermediary|mixin|eventbus|modlauncher|securejarhandler|bootstraplauncher)", jar.group(1)):
            return jar.group(1)
    return _guess_mod_from_class(cls)


def find_suspects(text: str, findings: list[Finding]) -> list[dict]:
    scores: Counter = Counter()
    why: dict[str, str] = {}

    names: dict[str, str] = {}
    installed_names = {m.group("id"): m.group("name").strip() for m in _FORGE_REPORT_MOD.finditer(text)}
    installed_names.update({mid: name for mid, name, _v in _FABRIC_REPORT_MOD.findall(text)})

    def add(mod: str | None, points: float, reason: str):
        if not mod or mod.lower() in _IGNORED_STACK_MODS or mod == "Minecraft":
            return
        mod = installed_names.get(mod, mod)
        key = re.sub(r"[^a-z0-9]", "", mod.lower())
        if not key:
            return
        # keep the nicest spelling ("Ice and Fire" rather than "iceandfire")
        if key not in names or (mod != mod.lower() and names[key] == names[key].lower()):
            names[key] = mod
        scores[key] += points
        why.setdefault(key, reason)

    for m in _FORGE_FAILED_MOD.finditer(text):
        add(m.group("name").strip(), 6, "failed to load")
    for mod in _ENTRYPOINT.findall(text):
        add(mod, 5, "its start-up code crashed")
    for f in findings:
        if f.key.startswith("tick:") and f.key.count(":") == 1:
            add(f.key[5:], 6, "its block/entity crashed")
        elif f.key.startswith("mixin:"):
            add(f.key[6:], 4, "its mixin failed")
    mod_ids = set(extract_mods(text)) | set(_ENTRYPOINT.findall(text)) | \
        {m.group("id") for m in _FORGE_FAILED_MOD.finditer(text)}
    mod_ids -= {"minecraft", "forge", "neoforge", "fabricloader", "java", "fabric-api"}
    frames = _FRAME.findall(_stack_after_root(text))[:30]
    seen_first = False
    for i, (cls, bracket) in enumerate(frames):
        mod = _frame_mod(cls, bracket, mod_ids)
        if mod:
            add(mod, 3.0 * (0.85 ** i) + (1.5 if not seen_first else 0), "at the top of the error" if not seen_first
                else "in the error's stack trace")
            seen_first = True
    for mod in _MIXIN_STACK.findall(text):
        add(mod, 1.0, "changes the crashing code (mixin)")
    out = []
    for key, score in scores.most_common(3):
        likelihood = "very likely" if score >= 5 else "likely" if score >= 3 else "possible"
        out.append({"mod": names[key], "likelihood": likelihood, "score": round(score, 1), "why": why[key]})
    return out


RULES = [
    rule_fabric_dependencies,
    rule_forge_dependencies,
    rule_missing_library,
    rule_mod_failed,
    rule_language_provider,
    rule_wrong_side,
    rule_broken_jar,
    rule_ticking,
    rule_java_version,
    rule_duplicates,
    rule_memory,
    rule_wrong_loader,
    rule_mixin,
    rule_config,
    rule_graphics,
    rule_server,
    rule_optifine,
    rule_shader,
    rule_world,
    rule_vram,
    rule_illegal_access,
    rule_missing_code,
    rule_connector,
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
    if any(f.title == "Missing library mod" for f in unique):
        unique = [f for f in unique if f.title != "Code not found"]
    unique.sort(key=lambda f: -f.severity)
    return {
        "info": extract_info(text),
        "findings": unique,
        "root_error": find_root_error(text),
        "stack_mods": find_stack_mods(text),
        "suspects": find_suspects(text, unique),
        "mods": extract_mods(text),
        "looks_like_crash": bool(_ERROR_HINT.search(text)),
    }
