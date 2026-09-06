# BlueOcean Memory

Shared, persistent memory for a project, reachable by every coding agent on the
machine through one MCP server. Its purpose is that work survives switching
agents or running out of context in one of them.

## Language

### What is stored

**Project**:
The scope every stored thing belongs to; one project, one collection. Named for
the working directory unless the caller says otherwise.
_Avoid_: repo, workspace, collection

**Entry**:
One stored unit of knowledge, carrying its own content, condensed summary, and
importance. The thing a search returns and an agent reads.
_Avoid_: record, memory, document, point

**Area**:
The broad subject an Entry belongs to, and the first scope a search narrows by.
_Avoid_: category, topic, domain

**Module**:
The specific subject within an Area, and the second scope a search narrows by.
An Area plus a Module names one thread of work.
_Avoid_: subarea, component, tag

### Records of a session

**Session Summary**:
An Entry written by an agent recording what was decided during a session and
why. Its value is the reasoning it carries, which nothing else can reconstruct.
_Avoid_: summary, session notes, handoff

**Session Trace**:
A record composed by the server of what a session touched — which tools ran,
which Areas and Modules were involved, which Entries were retrieved and stored,
what failed. It says what happened, never what was decided, and is never a
substitute for a Session Summary.
_Avoid_: summary, mechanical summary, session log, audit

### Sessions and their ending

**Session**:
One stretch of work by one agent on one Project. Not a protocol object: some
clients supply a usable session identity and some do not, so a Session is
whatever the design can attribute work to, never something the transport
guarantees.
_Avoid_: conversation, connection, run, thread

**Session End**:
The moment a Session's work is finished. Observable to the server and to some
clients, never to the agent itself, which is why nothing may be asked of the
agent at that moment.
_Avoid_: disconnect, close, termination

### How a mechanism is layered

**Floor**:
The tier of a mechanism that always runs, asks nothing of anyone, and must be
able to carry the mechanism alone. What the design rests on.
_Avoid_: fallback, default, baseline

**Ceiling**:
The tier that produces a better result when circumstances allow, and that the
design tolerates but never assumes. Never load-bearing.
_Avoid_: upgrade, enhancement, best case

**Save-as-you-go**:
Writing memory continuously while work happens rather than at an ending. The
Floor of this design, chosen because an ending is not a moment any agent is
present for.
_Avoid_: incremental save, autosave, continuous summarisation
