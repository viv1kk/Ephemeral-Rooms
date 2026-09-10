import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Editor } from '../editor/Editor';
import { FilePanel } from '../files/FilePanel';
import { formatBytes } from '../files/upload';
import { Presence, Toasts } from '../presence/Presence';
import { ShareMenu } from '../ui/ShareMenu';
import { useRoom } from './useRoom';

export function Room(): JSX.Element {
  const { roomCode = '' } = useParams();
  const navigate = useNavigate();
  const room = useRoom(roomCode);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [draftName, setDraftName] = useState('');
  const [noticeSeen, setNoticeSeen] = useState(false);
  // Narrow screens cannot show the sidebar and the editor at once, so they
  // swap between them. Above the breakpoint this value is inert.
  const [pane, setPane] = useState<'editor' | 'room'>('editor');

  // A one-time notice, because an unknown code silently creates a room and a
  // mistype would otherwise be indistinguishable from an expired one
  // (spec section 26).
  useEffect(() => {
    if (room.roomWasCreated && !noticeSeen) setNoticeSeen(true);
  }, [room.roomWasCreated, noticeSeen]);

  const leave = () => {
    room.leave();
    navigate('/');
  };

  // On a phone the document list and the editor are different screens, so
  // choosing a document has to move you to the one you just chose.
  const openDocument = (documentId: string) => {
    room.selectDocument(documentId);
    setPane('editor');
  };

  const newDocument = () => {
    room.createDocument('untitled.txt');
    setPane('editor');
  };

  const active = room.documents.find((d) => d.documentId === room.activeDocumentId) ?? null;
  const statusLabel =
    room.status === 'connected'
      ? 'Connected'
      : room.status === 'reconnecting'
        ? 'Reconnecting...'
        : room.status === 'connecting'
          ? 'Connecting...'
          : 'Disconnected';

  return (
    <div className="room">
      <header className="room-header">
        <h1>Room {roomCode}</h1>
        <div className="room-header-actions">
          <ShareMenu url={window.location.href} />
          <button className="danger" onClick={leave} data-testid="leave-room">
            Leave Room
          </button>
        </div>
      </header>

      {room.roomWasCreated && (
        <div className="notice" data-testid="new-room-notice">
          This room was empty, so a new one was created.
        </div>
      )}

      <nav className="pane-tabs" aria-label="Room sections">
        <button
          aria-current={pane === 'editor'}
          onClick={() => setPane('editor')}
          data-testid="tab-editor"
        >
          Editor
        </button>
        <button
          aria-current={pane === 'room'}
          onClick={() => setPane('room')}
          data-testid="tab-room"
        >
          Documents &amp; Files
        </button>
      </nav>

      <div className="room-body" data-pane={pane}>
        <aside className="room-sidebar">
          <section className="panel">
            <h2>Documents</h2>
            <ul className="doc-list" data-testid="document-list">
              {room.documents.map((doc) => (
                <li
                  key={doc.documentId}
                  className={doc.documentId === room.activeDocumentId ? 'active' : ''}
                >
                  {renaming === doc.documentId ? (
                    <form
                      onSubmit={(e) => {
                        e.preventDefault();
                        room.renameDocument(doc.documentId, draftName);
                        setRenaming(null);
                      }}
                    >
                      <input
                        autoFocus
                        value={draftName}
                        maxLength={room.limits?.maxDocNameChars ?? 128}
                        onChange={(e) => setDraftName(e.target.value)}
                        onBlur={() => setRenaming(null)}
                      />
                    </form>
                  ) : (
                    <>
                      <button
                        className="doc-name"
                        onClick={() => openDocument(doc.documentId)}
                        title={doc.name}
                      >
                        {doc.name}
                      </button>
                      <span className="doc-actions">
                        <button
                          className="link"
                          onClick={() => {
                            setRenaming(doc.documentId);
                            setDraftName(doc.name);
                          }}
                        >
                          Rename
                        </button>
                        <button
                          className="link danger"
                          onClick={() => room.deleteDocument(doc.documentId)}
                        >
                          Delete
                        </button>
                      </span>
                    </>
                  )}
                </li>
              ))}
            </ul>
            <button
              className="primary"
              data-testid="new-document"
              onClick={newDocument}
            >
              + New Document
            </button>
          </section>

          <Presence users={room.users} selfId={room.identity?.userId ?? null} />

          {room.identity !== null && room.limits !== null && (
            <FilePanel
              roomCode={roomCode}
              userId={room.identity.userId}
              chunkBytes={room.limits.uploadChunkBytes}
              files={room.files}
              remoteUploads={room.remoteUploads}
              maxUploadsPerUser={room.limits.maxUploadsPerUser}
              onDelete={room.deleteFile}
              onBroadcastProgress={room.broadcastUploadProgress}
            />
          )}

          <section className="panel room-info">
            <h2>About this room</h2>
            {/* Stated once, plainly, because the open-access model is by
                design and not a bug (spec section 21.1). */}
            <p className="warning">
              Anyone with this room code can view, edit, and delete everything here.
            </p>
            <label className="name-field">
              Your name
              <input
                value={room.identity?.displayName ?? ''}
                maxLength={room.limits?.maxDisplayNameChars ?? 32}
                onChange={(e) => room.setDisplayName(e.target.value)}
                data-testid="display-name"
              />
            </label>
          </section>
        </aside>

        <main className="room-main">
          {room.collab !== null && active !== null && room.identity !== null ? (
            <Editor
              key={room.collab.documentId}
              collab={room.collab}
              documentName={active.name}
              userName={room.identity.displayName}
              userColor={room.identity.color}
            />
          ) : (
            <div className="empty-editor">
              <p>No document open.</p>
              <button className="primary" onClick={() => room.createDocument('untitled.txt')}>
                + New Document
              </button>
            </div>
          )}
        </main>
      </div>

      <footer className="room-status">
        <span className={`status ${room.status}`}>
          <span className="dot" aria-hidden /> {statusLabel}
        </span>
        <span>
          {room.users.length} {room.users.length === 1 ? 'person' : 'people'}
        </span>
        {/*
          * What is left, not what is allowed. Nothing caps a document's length
          * or a file's size any more, so these two figures are the only answer
          * to "how much more can this room take" - which is why they are in the
          * status bar and not buried in a help page. Memory is omitted rather
          * than shown as zero when the server could not measure it.
          */}
        <span>Storage left: {formatBytes(room.storageAvailable)}</span>
        {room.memoryAvailable >= 0 && (
          <span>Memory left: {formatBytes(room.memoryAvailable)}</span>
        )}
      </footer>

      <Toasts toasts={room.toasts} onDismiss={room.dismissToast} />
    </div>
  );
}
