"""Tag collection helpers."""


def collect_tags(tag, acc=[]):
    acc = list(acc)
    acc.append(tag)
    return acc
