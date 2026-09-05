import path from 'node:path'

import { describe, expect, it } from 'vitest'

import { stateDbPreflightHomes } from './preflight-state-db'

const HOME = '/hermes'

// Minimal fs stub: `dirs` are the directories that exist, `files` the readdir
// listing per directory, and `notDirs` the entries whose statSync reports a
// non-directory (a stray file living under profiles/).
function fakeFs(opts: {
  dirs?: string[]
  listing?: Record<string, string[]>
  notDirs?: string[]
  throwReaddir?: boolean
  throwStat?: string[]
}) {
  const dirs = new Set(opts.dirs || [])
  const notDirs = new Set(opts.notDirs || [])
  const throwStat = new Set(opts.throwStat || [])

  return {
    existsSync: (p: string) => dirs.has(p),
    readdirSync: (p: string) => {
      if (opts.throwReaddir) {
        throw new Error('EACCES')
      }

      return (opts.listing || {})[p] || []
    },
    statSync: (p: string) => {
      if (throwStat.has(p)) {
        throw new Error('ENOENT')
      }

      return { isDirectory: () => !notDirs.has(p) }
    }
  }
}

describe('stateDbPreflightHomes', () => {
  it('returns only the root home when there is no profiles/ directory', () => {
    const fs = fakeFs({ dirs: [] })

    expect(stateDbPreflightHomes(HOME, { fs })).toEqual([HOME])
  })

  it('includes the root home first, then every profile home', () => {
    const profilesDir = path.join(HOME, 'profiles')

    const fs = fakeFs({
      dirs: [profilesDir],
      listing: { [profilesDir]: ['work', 'research', 'personal'] }
    })

    expect(stateDbPreflightHomes(HOME, { fs })).toEqual([
      HOME,
      path.join(profilesDir, 'personal'),
      path.join(profilesDir, 'research'),
      path.join(profilesDir, 'work')
    ])
  })

  it('sorts profiles for stable ordering regardless of readdir order', () => {
    const profilesDir = path.join(HOME, 'profiles')

    const fs = fakeFs({
      dirs: [profilesDir],
      listing: { [profilesDir]: ['zeta', 'alpha', 'mid'] }
    })

    expect(stateDbPreflightHomes(HOME, { fs })).toEqual([
      HOME,
      path.join(profilesDir, 'alpha'),
      path.join(profilesDir, 'mid'),
      path.join(profilesDir, 'zeta')
    ])
  })

  it('skips non-directory entries under profiles/ (stray files)', () => {
    const profilesDir = path.join(HOME, 'profiles')
    const strayFile = path.join(profilesDir, '.DS_Store')

    const fs = fakeFs({
      dirs: [profilesDir],
      listing: { [profilesDir]: ['work', '.DS_Store'] },
      notDirs: [strayFile]
    })

    expect(stateDbPreflightHomes(HOME, { fs })).toEqual([HOME, path.join(profilesDir, 'work')])
  })

  it('skips dot-prefixed entries such as the profiles/.deleted tombstone dir', () => {
    const profilesDir = path.join(HOME, 'profiles')

    const fs = fakeFs({
      dirs: [profilesDir],
      listing: { [profilesDir]: ['.deleted', 'work', '.stash'] }
    })

    expect(stateDbPreflightHomes(HOME, { fs })).toEqual([HOME, path.join(profilesDir, 'work')])
  })

  it('falls back to the root home when profiles/ cannot be read', () => {
    const profilesDir = path.join(HOME, 'profiles')
    const fs = fakeFs({ dirs: [profilesDir], throwReaddir: true })

    expect(stateDbPreflightHomes(HOME, { fs })).toEqual([HOME])
  })

  it('skips an entry whose statSync throws without dropping the others', () => {
    const profilesDir = path.join(HOME, 'profiles')
    const racing = path.join(profilesDir, 'racing')

    const fs = fakeFs({
      dirs: [profilesDir],
      listing: { [profilesDir]: ['work', 'racing'] },
      throwStat: [racing]
    })

    expect(stateDbPreflightHomes(HOME, { fs })).toEqual([HOME, path.join(profilesDir, 'work')])
  })
})
