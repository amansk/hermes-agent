import fs from 'node:fs'
import path from 'node:path'

/**
 * Enumerate every `hermesHome`-shaped directory whose `state.db` must be
 * pre-flighted (header-verified + emergency-backed-up) before a desktop update
 * touches the tree.
 *
 * The pre-flight guard (#68474) originally ran against `$HERMES_HOME/state.db`
 * only. Multi-profile installs keep a separate database per profile under
 * `$HERMES_HOME/profiles/<name>/state.db`, and those were left completely
 * unprotected: on a 13-database install (root + 12 profiles) the update window
 * backed up 1 of 13, so a profile whose backend was SIGKILL'd mid-update could
 * lose its database with no emergency copy to recover from (#97994).
 *
 * This helper returns the full set of homes to guard so the caller can run the
 * existing per-home pre-flight against each one. The root home comes first
 * (preserving the historical behaviour and log ordering), followed by every
 * profile home in stable sorted order. Because the per-home pre-flight prunes
 * its own emergency backups, iterating here keeps each database's backup count
 * bounded independently — total growth stays linear in the profile count rather
 * than unbounded.
 *
 * Only real profile homes count: dot-prefixed entries are skipped, matching the
 * Python side's definition (`named_profile_home` in hermes_constants.py) and
 * keeping the `profiles/.deleted` tombstone directory from producing a
 * misleading "state.db not found (fresh install?)" line on every update.
 *
 * Enumeration is fail-soft: a missing `profiles/` directory (single-profile or
 * fresh install), an unreadable directory, or an entry that cannot be stat'd
 * never throws. A profile we cannot enumerate simply falls back to being
 * unguarded exactly as before — the guard must never itself abort the update.
 */
export function stateDbPreflightHomes(hermesHome: string, options: any = {}): string[] {
  const fsImpl = options.fs || fs
  const homes = [hermesHome]
  const profilesDir = path.join(hermesHome, 'profiles')

  let entries: string[]

  try {
    if (!fsImpl.existsSync(profilesDir)) {
      return homes
    }

    entries = fsImpl.readdirSync(profilesDir)
  } catch {
    return homes
  }

  for (const name of entries.slice().sort()) {
    if (name.startsWith('.')) {
      continue
    }

    const home = path.join(profilesDir, name)

    try {
      if (fsImpl.statSync(home).isDirectory()) {
        homes.push(home)
      }
    } catch {
      // Unreadable/racing entry — skip it rather than abort the whole guard.
      void 0
    }
  }

  return homes
}
