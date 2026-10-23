#!/bin/sh
PRIVACY_RE="indi""tex|marcos""srud|msan""chez|ext.indi""tex.com|marcos.san""chez@|marcos.san""chez/|audit""qc|sustaud""qc|logistica""arteixo|ka""ia|/Users/marcos.san""chez"
zero=0000000000000000000000000000000000000000
base_ref=$(git symbolic-ref --quiet refs/remotes/origin/HEAD 2>/dev/null || echo refs/remotes/origin/main)
fail=0
while read local_ref local_sha remote_ref remote_sha; do
  [ "$local_sha" = "$zero" ] && continue
  if [ "$remote_sha" != "$zero" ]; then
    base=$remote_sha
  else
    base=$(git merge-base "$local_sha" "$base_ref" 2>/dev/null)
    [ -z "$base" ] && base=$(git rev-list --max-parents=0 "$local_sha" | tail -1)
  fi
  bad=$(git log --format='%H' "$base..$local_sha" | while read c; do
    git show --format= --unified=0 $c -- 2>/dev/null | grep -i -E '^\+' | grep -i -E "$PRIVACY_RE" | sed "s/^/  $c: /"
  done)
  if [ -n "$bad" ]; then
    echo "privacy: pushed content contains employer identifiers:"
    echo "$bad"
    fail=1
  fi
done
[ "$fail" = 1 ] && exit 1
exit 0
