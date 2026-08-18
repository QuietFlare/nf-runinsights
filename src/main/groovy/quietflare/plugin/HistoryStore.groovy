/*
 * Copyright 2026, quietflare
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
package quietflare.plugin

import java.nio.file.Files
import java.nio.file.Path

import groovy.json.JsonOutput
import groovy.json.JsonSlurper
import groovy.util.logging.Slf4j
import nextflow.file.FileHelper

/**
 * Durable cross-run history.
 *
 * Layout: one JSON file per run (`<timestamp>-<runName>.json`) inside a
 * history directory. A run file is created once and never appended -
 * that makes the store safe for concurrent runs on shared filesystems
 * (two simultaneous finishes can't interleave writes) and compatible
 * with object storage, where append does not exist. Paths resolve
 * through Nextflow's filesystem layer, so a team can point `history`
 * at a shared directory or an `s3://bucket/prefix` and every launch
 * host contributes to the same history.
 *
 * Older plugin versions wrote a single append-only `history.jsonl`;
 * when one is found it is still read (never written) so existing
 * history keeps counting toward comparisons.
 */
@Slf4j
class HistoryStore {

    final Path dir
    final Path legacyFile   // may be null

    HistoryStore(Path dir, Path legacyFile) {
        this.dir = dir
        this.legacyFile = legacyFile
    }

    /**
     * Work out where history lives from the `runinsights.history` config
     * value. Accepts a directory path (normal case), a path to an old
     * history.jsonl file (legacy configs keep working), or nothing
     * (default under the user's home).
     */
    static HistoryStore resolve(String configured) {
        if( configured ) {
            Path p = FileHelper.asPath(configured)
            if( Files.isRegularFile(p) )
                return new HistoryStore(p.parent.resolve('history'), p)
            return new HistoryStore(p, existingOrNull(p.parent?.resolve('history.jsonl')))
        }
        Path base = FileHelper.asPath(System.getProperty('user.home')).resolve('.nf-runinsights')
        return new HistoryStore(base.resolve('history'), existingOrNull(base.resolve('history.jsonl')))
    }

    private static Path existingOrNull(Path p) {
        p != null && Files.exists(p) ? p : null
    }

    /** Persist one run summary as its own file. */
    void save(Map runRecord) {
        Files.createDirectories(dir)
        // "2026-08-17T15:10:09.33+02:00" -> "20260817T151009", sortable and
        // filesystem-safe on every provider, including object storage
        String stamp = "${runRecord.ts}".replaceAll(/[^0-9T]/, '').take(15)
        String name = "${stamp}-${runRecord.run_name ?: 'run'}.json"
        Files.writeString(dir.resolve(name), JsonOutput.toJson(runRecord))
    }

    /** All recorded runs of the given pipeline, oldest first. */
    List<Map> load(String pipeline) {
        def out = []
        def slurper = new JsonSlurper()

        if( legacyFile != null && Files.exists(legacyFile) ) {
            Files.readAllLines(legacyFile).each { line ->
                if( !line.trim() ) return
                try {
                    def rec = slurper.parseText(line)
                    if( rec instanceof Map && rec.pipeline == pipeline )
                        out << rec
                }
                catch( Exception ignored ) { }   // one corrupt line never hides the rest
            }
        }

        if( Files.isDirectory(dir) ) {
            Files.list(dir).withCloseable { stream ->
                stream.sorted().each { Path f ->
                    if( !f.fileName.toString().endsWith('.json') ) return
                    try {
                        def rec = slurper.parse(Files.newBufferedReader(f))
                        if( rec instanceof Map && rec.pipeline == pipeline )
                            out << rec
                    }
                    catch( Exception e ) {
                        log.debug "nf-runinsights: skipping unreadable history file ${f}: ${e.message}"
                    }
                }
            }
        }

        // legacy and per-run entries interleave chronologically
        return out.sort { "${it.ts}" }
    }
}
