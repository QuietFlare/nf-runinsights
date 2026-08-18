package quietflare.plugin

import java.nio.file.Files
import java.nio.file.Path

import spock.lang.Specification

class HistoryStoreTest extends Specification {

    Path tmp

    def setup() { tmp = Files.createTempDirectory('runinsights-test') }

    def cleanup() { tmp.toFile().deleteDir() }

    private static Map run(String pipeline, String name, String ts) {
        [ts: ts, run_name: name, pipeline: pipeline,
         processes: [A: [tasks: 1, failed: 0, realtime_ms_median: 1000]]]
    }

    def 'saves one file per run and loads them back oldest first'() {
        given:
        def store = new HistoryStore(tmp.resolve('history'), null)

        when:
        store.save(run('toy', 'second', '2026-08-17T15:00:00+02:00'))
        store.save(run('toy', 'first', '2026-08-17T14:00:00+02:00'))
        store.save(run('other', 'unrelated', '2026-08-17T14:30:00+02:00'))

        then:
        Files.list(tmp.resolve('history')).count() == 3

        and:
        def loaded = store.load('toy')
        loaded*.run_name == ['first', 'second']
    }

    def 'merges a legacy jsonl file into results'() {
        given: 'an old-format append file plus one new-format run'
        def legacy = tmp.resolve('history.jsonl')
        Files.writeString(legacy,
            '{"ts":"2026-08-17T10:00:00+02:00","run_name":"old_run","pipeline":"toy","processes":{}}\n' +
            'not json at all\n')
        def store = new HistoryStore(tmp.resolve('history'), legacy)
        store.save(run('toy', 'new_run', '2026-08-17T16:00:00+02:00'))

        when:
        def loaded = store.load('toy')

        then: 'legacy entry counts, corrupt line skipped, order chronological'
        loaded*.run_name == ['old_run', 'new_run']
    }

    def 'skips unreadable per-run files'() {
        given:
        def store = new HistoryStore(tmp.resolve('history'), null)
        store.save(run('toy', 'good', '2026-08-17T15:00:00+02:00'))
        Files.writeString(tmp.resolve('history/zz-broken.json'), '{{{')

        expect:
        store.load('toy')*.run_name == ['good']
    }

    def 'resolve treats an existing file path as legacy and writes beside it'() {
        given: 'a config still pointing at the old history.jsonl'
        def legacy = tmp.resolve('history.jsonl')
        Files.writeString(legacy, '')

        when:
        def store = HistoryStore.resolve(legacy.toString())

        then:
        store.legacyFile == legacy
        store.dir == tmp.resolve('history')
    }

    def 'resolve treats a non-file path as the history directory'() {
        when:
        def store = HistoryStore.resolve(tmp.resolve('team-history').toString())

        then:
        store.dir == tmp.resolve('team-history')
        store.legacyFile == null
    }

    def 'resolve picks up a sibling legacy file next to a configured directory'() {
        given: 'old file exists beside the new directory location'
        def legacy = tmp.resolve('history.jsonl')
        Files.writeString(legacy, '')

        when:
        def store = HistoryStore.resolve(tmp.resolve('history').toString())

        then:
        store.legacyFile == legacy
    }
}
