// Toy pipeline for exercising nf-runinsights locally.
// Run it a few times to build history, then trigger a deliberate
// regression with:  nextflow run main.nf --slow
params.slow = false

process PREP {
    output:
    path 'data.txt'

    script:
    """
    sleep 1
    seq 1 100 > data.txt
    """
}

process CRUNCH {
    input:
    path x

    output:
    path 'result.txt'

    script:
    """
    sleep ${params.slow ? 8 : 2}
    wc -l ${x} > result.txt
    """
}

process REPORT {
    input:
    path r

    output:
    stdout

    script:
    """
    cat ${r}
    """
}

workflow {
    PREP()
    CRUNCH(PREP.out)
    REPORT(CRUNCH.out)
    REPORT.out.view()
}
