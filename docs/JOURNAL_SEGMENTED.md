---
title: Segmented Journal File Format
category: Interfaces
layout: default
SPDX-License-Identifier: LGPL-2.1-or-later
---

# Segmented Journal File Format

This document describes the segmented variant of the journal file format.
It builds on [Journal File Format](JOURNAL_FILE_FORMAT). Read that first.
The name comes from its central idea: nothing is ever written in place, and the log is cut into segments that each get an index once they are complete.

Status: experimental. New files use this format if `SYSTEMD_JOURNAL_SEGMENTED=1` is set. The classic format stays the default.
Files in this format have the `HEADER_INCOMPATIBLE_SEGMENTED` header flag, so older versions of systemd refuse to open them.

## The Problem

A classic journal file is a set of linked structures that are updated in place. Appending one entry changes:

* the header: counters, the tail entry, and the arena size,
* a hash table bucket or chain link for each new value and each new field,
* the entry array of the file,
* for each value of the entry, the entry array of that value and its entry count.

These locations are spread all over the file, and they are written through a shared writable memory map.
Each writeback of dirty pages hence consists of many small, scattered writes. On a desktop journal the classic format writes back about 370 dirty pages in about 160 separate ranges per writeback,
and several times more bytes than the payload it stores (see "Measurements").

## The Idea

Nothing is changed once it is written. The file is a log: appending an entry writes new objects at the end, and nothing else.

The structures that the classic format updates in place exist to find entries: by value, by position, and by time.
The segmented format replaces them with *indexes*.
The writer builds an index in memory and appends it in one piece every few thousand objects.
Readers use the indexes for most of the file, and read the few objects after the newest index directly.
When a file is archived, its indexes are merged into one.

```
Header | DATA ENTRY DATA ENTRY ... | INDEX DATA CONTEXT ENTRY ... | INDEX DATA ENTRY ... MARK ENTRY ...
         \_______ segment 1 _______/ \_________ segment 2 ________/ \_______ tail (segment 3) ______/
```

## Objects Are Immutable

The object types that exist to be updated are gone: field objects, hash tables, and entry arrays.
`DATA` objects hold one `FIELD=value` payload and its hash, without links to other objects or counters.
`ENTRY` objects hold the sequence number, timestamps, and boot ID of an entry, and items that refer to its data.
`TAG` objects are used for sealing, as before. `CONTEXT`, `INDEX`, and `MARK` objects are new, and explained below. For all objects:

* An object is written once, at the end of the file, and never changed.
* An object only refers to objects at lower offsets. Data objects are written before the entry that uses them.
* Each object has a checksum, in bytes that are reserved in the classic object header.
  A reader that scans the log accepts an object only if it lies completely within the file size and its checksum matches.
  The file size only grows after the bytes are written, so a live reader never sees a partial object, without any locking.
  After a crash, the checksum tells an intact object from one that was cut short.
* The header is written once, when the file is created.
  It holds the file ID, the machine ID, the sequence number ID, and the sequence number that the file continues from.
  Its other counters and tail fields stay 0. Readers learn them from the log.
  Since the header is never rewritten, the sequence number ID is fixed when the file is created.
  The writer refuses entries with a different one, where the classic format adopts the ID of the first entry.

The checksum is keyed with the file ID and includes the offset of the object, so an object copied from another file or offset does not validate.
That matters, because a payload is arbitrary bytes and may look like an object.
The checksum of a data object does not cover its payload. The hash of the data object does.
Readers do not check that hash, so after a crash they do not notice a payload that was written after the last sync and is damaged,
while the objects around it are intact. The same is true for the classic format. `journalctl --verify` detects it.

## Segments and Checkpoints

A *segment* is the part of the log that one index covers, or will cover.
It starts at the previous index, or at the end of the header, and ends where its own index starts.
The *tail* is the segment that is still being filled: the newest index and everything after it.
Within a segment, the writer stores each distinct value once, like the classic format does for the whole file.
The hash table for that lives in memory, not in the file.
The writer keeps the *segment state* in memory: for each distinct value of the tail the offset of its data object, and the entries that have it.
For a new entry, the writer writes data objects only for the values that the segment does not have yet, followed by the entry object, with one `pwritev()` call.

A *checkpoint* ends the segment: the writer turns the segment state into an index, appends it, and starts a new segment.
At a checkpoint the writer forgets its values, so that its memory, and the work to rebuild it when a writer continues the file, stay bounded.
A value that occurs again after a checkpoint is hence stored again, except for large values (16 KiB and more, the last 64 of them).

A checkpoint happens when the file is archived. It also happens after an entry if the tail holds 8 MiB, or a certain number of objects:
one per 8 KiB of the maximum file size, but at least 1,024 and at most 16,384.
This bounds the tail, which a writer reads again when it continues a file.

## Indexes

An index answers the questions that the classic format answers with its hash tables and entry arrays, for the entries of one segment:

* Which entries are in the segment? The *entry array* lists their offsets, in order.
* Which fields occur? The *field table* lists their names.
* Which entries have `FIELD=value`? The *data table* has one item per distinct value.
  Values of a few fields are handled differently, see "Storage Classes".
  An item holds the offset of a data object with that value, and a *posting list* of the entries that have it.

The *ordinal* of an entry is its position among all entries of the file. Posting lists hold ordinals relative to the segment.
A posting list with a single entry is stored in the item itself.
Longer ones are stored run-length encoded, or as a bitmap if that is smaller.
A value that almost every entry has, such as `_BOOT_ID=`, needs a few bytes. One that every second entry has needs 1 bit per entry.

For example, this segment and its index:

```
entry 0: _SYSTEMD_UNIT=a.service PRIORITY=6    entry array: offsets of entry 0, 1, and 2
entry 1: _SYSTEMD_UNIT=b.service PRIORITY=6    field table: PRIORITY, _SYSTEMD_UNIT
entry 2: _SYSTEMD_UNIT=a.service PRIORITY=3    data table:  PRIORITY=3 -> 2, PRIORITY=6 -> 0 1,
                                                            _SYSTEMD_UNIT=a.service -> 0 2, _SYSTEMD_UNIT=b.service -> 1
```

Fields are sorted by the hash of their name, and the values of a field by their hash, so lookups are binary searches (the example shows names).
To find the entries with `_SYSTEMD_UNIT=a.service`, a reader looks up the field, then the value, and decodes the posting list.
It does not trust hashes alone: it compares the payload of the data object that the item names.
A data table item carries two independent 64-bit hashes of its value, which lets the writer merge indexes without reading payloads.

An index also records the state of the file at its position: the numbers of entries and objects,
the first and last sequence numbers, and the timestamps and boot ID of the last entry.
These are the header fields that the classic format updates in place.
Indexes form a chain: each records where its segment starts, which is the offset of the previous index.

Why one index per segment, and not one per file? An index of entries that are already written never has to change, and writing it is one sequential write.
The price is that a query on an active file looks each value up in each index.
Archived files make up most of the journal, and have one index (see "Archiving").

## Contexts

On a desktop journal an entry has about 25 fields. 16 of them are trusted metadata that journald adds, such as `_PID=`, `_UID=`, and `_SYSTEMD_UNIT=`.
They are the same for all entries of a process.
In 300,000 entries there are 3,844 distinct sets of such fields, and 99.3% of the entries use a set that occurs more than once.
A `CONTEXT` object stores such a set once, as a list of data objects. An entry refers to it with one item, instead of one item per field.

The writer collects the values of an entry whose field names start with `_`, except the `_SOURCE_*` timestamps, which differ for every entry.
The first time it sees a set in a segment, it only remembers it.
The second time, it writes a context object, and this entry and later entries of the segment with the same set refer to it.
Sets that occur only once cost nothing. A new segment starts without contexts.
Contexts only make entries smaller. The index still lists each value with all entries that have it, directly or through a context.

## Marks

The header is never rewritten, so it cannot tell readers where the newest index is. Scanning the whole file would be slow.
Also, an index that is in the page cache may not be on disk yet, and after a crash a reader must not use an index that was written in part.

A `MARK` object solves both problems. It names an index, and vouches that this index and all indexes before it in the chain are on disk completely.
journald syncs files periodically (every 5 minutes by default) with `fdatasync()` in a separate thread.
If the sync succeeded, the main thread appends a mark the next time it appends to, syncs, or closes the file.
After a failed sync, the writer appends no more marks, since the kernel reports a writeback error only once.
The mark names the newest index that existed when the sync started. If an earlier mark names that index already, no mark is appended.

A mark ends with a magic value, `JRNLMARK`. Readers find the newest mark by looking for it backwards from the end of the file.
A payload can contain the same bytes, so the mark only counts if a scan that starts at the index the mark names arrives at the mark as an object.
Otherwise the reader tries the next older mark. After 64 marks, or if there is none, it scans the file from the beginning.

The *final mark* is the last object of an archived file. It tells readers that the file does not change anymore, and writers that they must not continue it.
It replaces the `STATE_ARCHIVED` header state of the classic format.

## Reading

### Opening a File

1. Find the newest mark. Load the index it names and the indexes before it.
2. Scan the log after the newest index: check each object, and remember the offsets of the entries. This is the tail.
3. Stop at the first object that is not valid: in an active file, usually the end of the file, or an object that is still being written.

If the scan finds an index that continues the chain, or one that covers the whole file, the reader uses it too,
after checking a checksum over all of it, since no mark vouches for it.
The reader keeps a private copy of the header, and fills in the counters and tail fields from the newest index and the tail.

Opening hence reads every object after the index that the newest mark names.
Marks are only appended after syncs, so that is what was written since the last successful sync, plus at most one segment.
A file without a mark, for example one that was not synced since its first checkpoint, is read from the beginning.

An index is derived data. The scan skips a damaged index as long as its size is intact, while any other object that is not valid ends the scan.
When a reader loads an index that a mark vouches for, it only checks the fixed part, since checking the rest would mean reading the whole index on every open.
If the rest is damaged on disk, queries may return wrong results without noticing. Readers still do not crash or fail, and `journalctl --verify` detects the damage.
If a reader does notice that an index is inconsistent, for example because a posting list does not decode,
it stops using that index and those after it, and reads what they cover like the tail. That is slower, but no entry is lost.

### Finding Entries

The indexes and the tail together form an array of all entries, ordered by offset:

* By ordinal: a binary search over the indexes finds the one that covers the ordinal. Its entry array has the offset.
* By sequence number or realtime timestamp: a binary search over the ordinals, which reads the entry at each step.
  Like the classic format, it assumes that these increase with the offset.
* By monotonic timestamp: the same, but only among the entries that match `_BOOT_ID=`.

### Matches

`sd-journal` evaluates the whole match expression of a file into a bitmap with one bit per entry.
For each index it looks up each value of the expression and decodes the posting lists. `AND` and `OR` become bitwise operations.
The entries of the tail are read and checked against the expression.
Moving to the next matching entry is a search for the next set bit. `sd-journal` caches recent results.

Seeking to a timestamp with an `OR` expression can give a different first entry than the classic format.
The classic format bisects the entries of each term separately and takes the nearest result.
This format bisects all entries of the file, or all entries of the boot for monotonic timestamps, and then looks for the next match.
The two agree as long as the timestamps of the entries that each term matches increase with the offset.
In a file that interleaves the entries of several boots, for example from `systemd-journal-remote --split-mode=none`,
that is not true for monotonic timestamps.

Field names come from the field tables of the indexes and from the tail; the values of a field from the data tables and from the tail.
A value may occur in several indexes and files, so `sd-journal` removes duplicates with a set of keyed 128-bit hashes of what it returned.

### Following a Live File

A reader *refreshes* a file by calling `fstat()` and continuing the scan where it stopped.
It does so when it has reached the end of the file and is asked for the next entry, and when it seeks: at most every 10 ms, or right after an inotify event.
`pwritev()` triggers `IN_MODIFY` for every entry, while journald triggers it for classic files at most every 250 ms.
To avoid waking readers for every entry, `sd-journal` removes `IN_MODIFY` from the watch of a directory for up to 250 ms after an event for a segmented file in it.
A timer restores the watch and makes readers refresh their segmented files.
For that, `sd_journal_get_fd()` now returns an epoll file descriptor that combines the inotify descriptor and the timer,
for all journals, not only those with segmented files.

## Writing

### Appending

1. Look up each value in the segment state. A hit is confirmed by comparing the payload in the file.
   A miss creates a new data object, compressed if it is large enough.
2. Find or create the context.
3. Lay out the new data objects, the new context if any, and the entry, compute their checksums, and write them with one `pwritev()` call.
4. Update the segment state and the private copy of the header.

The writer never writes through the memory map. Space is preallocated in steps of 8 MiB with `FALLOC_FL_KEEP_SIZE`, so the file size only covers what was written.
The writer keeps room for the merged index that archiving writes. It estimates its size as the size of the existing indexes plus 1/8 of the tail.
An append that would eat into that room fails, and journald rotates.
If a write fails before anything reached the file, the file stays usable, except when the write was a tag of a sealed file.
If part of an object reached the file, nothing can be appended after it anymore: the writer refuses further appends, and journald rotates.

### Crash Recovery

After a crash everything up to the last successful `fdatasync()` is on disk, and what was written later may be there in part.
Readers see the log up to the first object that is not valid.
A writer that opens an existing file first loads it like a reader. It refuses to continue the file if the scan did not end exactly at the end of the file.
It also refuses it if the tail holds more than 32,768 entries or 16 MiB, twice the most that any checkpoint allows. Then an index is probably missing or damaged.
It also refuses archived files, and sealed files that do not end with a tag.
journald then moves the file aside and starts a new one, as it does for classic files that were not closed cleanly.
Otherwise the writer rebuilds the segment state from the tail: it reads each data object, checks its hash, and replays the entries. If a hash does not match, it refuses the file as well.
Contexts in the tail are not used again. A file is never repaired or cut short.

### Archiving

Archiving renames the file as before, and runs a checkpoint so that all entries are indexed.
The main thread does not write to the file anymore. The rest happens when journald closes the file, usually in the separate thread that also syncs files:

1. If the file has more than one index and all entries are indexed, the indexes are merged into one that covers the whole file, and it is appended.
   All data tables are sorted the same way, so this is one pass over all of them.
   Ordinals are shifted, and posting lists are concatenated and encoded again.
2. `fdatasync()`.
3. The final mark is appended. It names the newest index if no sync of the file failed, otherwise the index that the previous mark named.
4. Preallocated space after the end of the file is released, and the file is synced with `fsync()`.

The replaced indexes stay in the file, unused. If building the merged index fails, for example for lack of memory, the file keeps its indexes.
If writing the merged index or the final mark fails, or the process dies while archiving, the file has no final mark,
like the file of a writer that crashed. It stays readable, and readers treat it like an active file.

## Storage Classes

In 300,000 entries of a desktop journal there are 365,204 distinct values, and 349,831 of them belong to `MESSAGE=` and the `*_TIMESTAMP=` fields.
Source timestamps are unique to their entry. 84% of the messages repeat, but the other 16% are unique, like source timestamps.
Indexing such values costs a data table item of 32 bytes per segment, and again in the merged index, for values that are rarely looked up.
The writer hence assigns each field a *storage class*:

| Class | Fields | Stored as | In the index |
|---|---|---|---|
| Indexed | all others | data object | data table item with posting list |
| Unindexed | `MESSAGE`, `SYSLOG_TIMESTAMP`, `SYSLOG_RAW`, `COREDUMP` | data object with the `OBJECT_UNINDEXED` flag | hash of the value |
| Inline | `_SOURCE_REALTIME_TIMESTAMP`, `_SOURCE_MONOTONIC_TIMESTAMP`, `_SOURCE_BOOTTIME_TIMESTAMP` | inside the entry object if smaller than 256 bytes, otherwise like unindexed | a flag on the field |

Unindexed values are deduplicated like indexed ones, but are never part of a context. Inline values are not deduplicated.
The list of fields is writer policy. Readers only go by what is in the file. A value of an indexed or unindexed field that exists in the segment already is reused with the class it has.

Two flags in the field table say whether a segment has unindexed or inline values of a field. A match on such a value may have to read entries:

* For an unindexed field, the index knows whether the hash of the value occurs in the segment, but not in which entries.
  If it occurs, all entries of the segment are candidates.
* For a field with inline values, all entries of the segment are candidates.

The match result is then a superset. A second bitmap records which candidates were checked, so results take 2 bits per entry instead of 1.
When iteration reaches a candidate that was not checked, it reads the entry and checks it against the whole expression.
An archived file usually has a single index, which covers all entries. There, a match on only an unindexed value hence reads all entries or none, and a match on only an inline value reads all entries.
Listing the unique values of such a field reads all objects of the segments that have it unindexed or inline.
No code in the systemd tree matches on these fields. `journalctl FIELD=value` and `systemd-journal-gatewayd` pass user supplied matches through.

On the same input, storage classes cut the bytes written back from 91 MiB to 62 MiB, and the size of all files from 88 MiB to 60 MiB.
Cold matches on messages went from 102 ms to 263 ms, and on source timestamps from 15 ms to 584 ms.

## Sealing and Verification

Forward secure sealing works as before.
The HMAC covers the data, context, entry, and tag objects in log order, in full except for the checksum and the tag itself.
Indexes and marks are derived data, like hash tables and entry arrays in the classic format, and are not covered.
Hence a crafted file can make a match return entries that do not have the value. The classic format has the same property.
Closing a file appends a tag, so an active sealed file whose last object, not counting marks, is not a tag was not closed cleanly.

Checksums detect incomplete and damaged objects, not forgeries. Anyone who can read a file knows its file ID, and can hence compute checksums.
Someone who can also get arbitrary bytes into a payload at a predictable offset can put a fake index and mark into that payload,
and readers accept them until the writer appends the next real mark.
A writer that continues the file after a restart builds on them, and the entries before them stay hidden.
`journalctl --verify` is not fooled, since it walks every object from the header.

`journalctl --verify` checks each object of the log: checksum, structure, padding, the hash of data objects, and the objects it refers to.
A mark must name an index that a reader would use at that point, or one that a merged index replaced, and the final mark must be the last object.
Each index that a reader would use is rebuilt from the log and compared: entries, fields, values, and posting lists.

## Trade-offs

* **32-bit offsets.** The format requires the compact and keyed hash flags.
  Offsets in entries, contexts, and indexes are 32 bits. That keeps them small, and limits files to 4 GiB, far above journald's default limits.
* **Checksums keyed by the file ID.** Together with the file size, they let readers recognize incomplete objects without locking, at the cost of hashing each object when it is written and when it is scanned.
  To keep that cheap, they do not cover the payload of data objects, which the hash covers, and of indexes, which have a separate checksum.
* **Index cadence.** Frequent checkpoints keep the tail short, so continuing a file after a restart is fast.
  Opening an active file is bounded by the sync interval instead, since readers start at the newest mark.
  But each index is one more lookup per value of a query, and values are stored once per segment instead of once per file.
  The object limit grows with the maximum file size between 8 MiB and 128 MiB, so in that range the number of indexes per file stays about the same.
* **Indexes are built in the main thread.** A checkpoint takes time proportional to the distinct values and entries of the segment.
  That is the maximum append latency in "Measurements". Merging at archiving time happens in the separate thread that also syncs files.
* **One system call per entry.** The writer uses `pwritev()` instead of writing to the memory map.
  That is one system call per entry, but all writes are sequential, and a page does not change once it is full. Only the last, partly filled page may be written back more than once.
* **Storage classes.** Not indexing messages and source timestamps makes files smaller, but matches on them have to read entries.

## Measurements

`test-journal-benchmark` is a manual test that writes the same entries into files of each format, emulating kernel writeback and journald syncs,
and then runs the same queries on each. It fails if the formats return different results.

`test-journal-benchmark --input=/var/log/journal/<machine-id> --entries=300000 --max-size=24M` on btrfs,
with 300,000 entries of a desktop journal (317 MiB of payload, about 1 day of log time).
Each format rotates into several files. Sizes and byte counts are totals over all of them.

| Writing | classic | compact | segmented |
|---|---|---|---|
| Bytes written back | 1140 MiB | 953 MiB | 62 MiB |
| Dirty pages per writeback | 368 | 307 | 21.3 |
| Dirty ranges per writeback | 156 | 125 | 1.0 |
| Size of all files | 264 MiB | 176 MiB | 60 MiB |
| Append CPU time per entry | 27.1 us | 16.0 us | 12.6 us |
| Append latency, maximum | 15.1 ms | 11.5 ms | 21.1 ms |
| Write time | 8.9 s | 5.5 s | 4.0 s |

| Reading, cold cache unless noted | classic | compact | segmented |
|---|---|---|---|
| Last 10 entries | 40.2 ms | 37.4 ms | 7.5 ms |
| Match on a rare unit | 53.3 ms | 40.7 ms | 26.9 ms |
| Match on `MESSAGE=` | 158 ms | 112 ms | 263 ms |
| Match on `_SOURCE_REALTIME_TIMESTAMP=` | 29 ms | 31 ms | 584 ms |
| Unique values of `_SYSTEMD_UNIT=` | 68 ms | 47 ms | 21 ms |
| `--grep` over all messages | 1099 ms | 1049 ms | 716 ms |
| Iterate all entries with all fields, warm cache | 2.74 s | 2.80 s | 2.94 s |
| Follow at 1,000 entries/s: wakeups per second | 4.0 | 4.0 | 7.6 |

## On-Disk Reference

`HEADER_INCOMPATIBLE_SEGMENTED` is bit 5. It requires `HEADER_INCOMPATIBLE_COMPACT` and `HEADER_INCOMPATIBLE_KEYED_HASH`.
The header layout is the same as for classic files.
In segmented files `state` is `STATE_OFFLINE`, and `seqnum_id` is the one the creator passes, otherwise the one of the file it replaces, otherwise the file ID.
`tail_entry_seqnum` is the sequence number that the file continues from. All other counters, `arena_size`, and all head, tail, and hash table fields are 0.

In the object header, `le16_t aux` and `le32_t checksum` take the place of the reserved bytes. Both are 0 in classic files.
`aux` is the number of items of entries and contexts, `MARK_FINAL` for the final mark, and 0 otherwise.
Objects are aligned to 8 bytes, padding is zero. A future object type needs a new incompatible flag, since the scan stops at unknown types.

| Type | Value | Minimum size | Covered by `checksum` |
|---|---|---|---|
| `DATA` | 1 | 25 | object header and `hash` |
| `ENTRY` | 3 | 72 | all |
| `TAG` | 7 | 64 | all |
| `CONTEXT` | 8 | 20 | all |
| `INDEX` | 9 | 160 | `struct IndexObject` without the payload |
| `MARK` | 10 | 32 | all |

* `checksum` is the lower 32 bits of `siphash24()` keyed with `file_id`, over the offset as `le64_t` and the covered part, with `checksum` taken as 0.
* `hash` is `siphash24()` keyed with `file_id`, over the uncompressed payload, or over the name for fields.
* `hash2` is `siphash24()` keyed with `file_id` XOR `6a6f75726e616c2d617070656e646f6e`, over the uncompressed payload.
* `payload_checksum` of an index is the lower 32 bits of `siphash24()` keyed with `file_id`, over the object from `payload` to its end.

A `DATA` object is the object header, `le64_t hash`, and the payload `FIELD=value` of at least 1 byte, possibly compressed.
Data objects of unindexed values have the flag `OBJECT_UNINDEXED` (bit 3), and the data table has no items for them.
A `CONTEXT` object is the object header followed by `aux` offsets (`le32_t`) of `DATA` objects, strictly ascending, at most 1024.

```c
struct IndexObject {
        ObjectHeader object;
        le64_t head_offset;     /* end of the header, or the offset of the previous index */
        le64_t n_objects, n_entries, n_data, n_tags;      /* the state of the file at the index */
        le64_t head_entry_seqnum, tail_entry_seqnum, head_entry_realtime, tail_entry_realtime, tail_entry_monotonic;
        sd_id128_t tail_entry_boot_id;
        le64_t tail_entry_offset;
        le32_t n_index_entries, entry_array_offset;     /* le32_t, the offsets of the entries, ascending */
        le32_t n_fields, field_table_offset;            /* IndexFieldItem, sorted by hash, then name */
        le32_t n_data_items, data_table_offset;         /* IndexDataItem, by field, then sorted by (hash, hash2) */
        le32_t n_unindexed, unindexed_offset;           /* le64_t, the hashes of the unindexed values, ascending */
        le32_t payload_checksum, reserved;
        uint8_t payload[];
};

struct IndexFieldItem {
        le64_t hash;
        le32_t name_offset, name_size;  /* the name, without "=" */
        le32_t flags;                   /* INDEX_FIELD_UNINDEXED (1), INDEX_FIELD_INLINE (2) */
        le32_t n_data, first_data;      /* the values of the field in the data table */
        le32_t reserved;
};

struct IndexDataItem {
        le64_t hash, hash2;
        le32_t data_offset;             /* a DATA object with this payload, possibly before head_offset */
        le32_t n_entries;
        le32_t postings_offset, postings_size;  /* the upper two bits of postings_size are the encoding */
};

struct MarkObject {
        ObjectHeader object;    /* aux: MARK_FINAL (1) for the final mark, otherwise 0 */
        le64_t index_offset;    /* an index that is on disk, or 0 */
        uint8_t magic[8];       /* "JRNLMARK" */
};
```

An `ENTRY` object is a compact classic entry object of `64 + ALIGN8(4 * aux)` bytes, plus its inline values.
An item is `offset | tag`, with the tag in the three low bits. Tag 0 is a `DATA` object, tag 1 a `CONTEXT` object whose data the entry has, at most one per entry.
Tag 2 is an inline value, with the offset relative to the entry object. Inline values follow the items, each aligned to 8 bytes:
a `le32_t` size of at least 1, then the payload, never compressed. Tags 3 to 7 are not used. Items ascend strictly.
`xor_hash` is the value a classic keyed hash file has: the XOR of `jenkins_hash64()` over the payloads that the entry was written with.

An index covers the log from `head_offset` up to its own offset, a range of at least one object.
The ordinal of its first entry is `n_entries - n_index_entries`. Section offsets are relative to the object and multiples of 8.
All `reserved` fields are 0.
The data table has one item per distinct `(hash, hash2)` among the indexed data that the entries refer to, directly or through contexts.
Posting lists hold ordinals relative to the index, ascending. Posting lists that are not inline follow each other in the order of the data table and do not overlap:

| Encoding | Format |
|---|---|
| 0, inline | `n_entries` is 1, `postings_offset` is the ordinal, and the size is 0. |
| 1, run-length | Pairs of LEB128 integers `(gap, length - 1)`, one per run of consecutive ordinals. For the first run, `gap` is its first ordinal. For later runs, it is the first ordinal minus the ordinal after the previous run, and at least 1, so runs never touch. |
| 2, bitmap | `ceil(n_index_entries / 64)` words of `le64_t`. Ordinal `i` is bit `i % 64`, from the least significant bit, of word `i / 64`. Bits for ordinals at or beyond `n_index_entries` are 0. |

A mark is valid if `size` is 32, `flags` is 0, `aux` is 0 or 1, the magic and the checksum match, and `index_offset` is 0 or a multiple of 8 between the end of the header and the mark.
