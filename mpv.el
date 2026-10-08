;;; mpv.el --- Controller for mpv       -*- lexical-binding: t; -*-

;; Copyright (C) 2026  Qiqi Jin

;; Author: Qiqi Jin <ginqi7@gmail.com>
;; Version: 1.0
;; URL: https://github.com/ginqi7/mpv.el
;; Package-Requires: ((emacs "29.1") (websocket-bridge "0.0.1") (vui "1.4.0"))
;; Keywords: multimedia, video

;; This program is free software; you can redistribute it and/or modify
;; it under the terms of the GNU General Public License as published by
;; the Free Software Foundation, either version 3 of the License, or
;; (at your option) any later version.

;; This program is distributed in the hope that it will be useful,
;; but WITHOUT ANY WARRANTY; without even the implied warranty of
;; MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
;; GNU General Public License for more details.

;; You should have received a copy of the GNU General Public License
;; along with this program.  If not, see <https://www.gnu.org/licenses/>.

;;; Commentary:

;; Talk to the mpv side over websocket-bridge.  The Python half lives in
;; mpv_bridge.py next to this file; it drives the player and pushes
;; notifications back through the functions below.
;;
;; The bridge is one-way per call: every command here returns immediately and
;; the result comes back later as one of the mpv-* functions, which the Python
;; side calls by name.  `mpv-update-video-info' fills in the last known file
;; metadata and `mpv-run-time-update-functions' runs the hooks that want a
;; per-second position.

;;; Code:

(require 'websocket-bridge)
(require 'dired)

(defgroup mpv nil
  "Control the mpv video player."
  :group 'applications
  :prefix "mpv-")

;;; Custom Variables

(defcustom mpv-py-path
  (concat (file-name-directory (or load-file-name (buffer-file-name)))
          "mpv_bridge.py")
  "Absolute path of the Python bridge script.

It is expected to sit next to this file."
  :type 'string
  :group 'mpv)

(defcustom mpv-python (executable-find "python3")
  "The Python interpreter used to run the bridge."
  :type 'string
  :group 'mpv)

(defcustom mpv-definition-function nil
  "Function that explains one hovered word.

It is called with the word and must return a string.  Left nil until
you set it, which is what `mpv-definition-word' checks for."
  :type 'function
  :group 'mpv)

(defcustom mpv-translation-function nil
  "Function that explains one right-clicked subtitle line.

It is called with the whole line and must return a string.  Left nil
until you set it, which is what `mpv-explain-text' checks for."
  :type 'function
  :group 'mpv)

;;; Internal Variables

(defvar mpv--video-info nil
  "Plist of metadata for the file mpv is playing.

Updated by `mpv-update-video-info'; nil until the first file loads.")

(defvar mpv--time-update-functions nil
  "List of functions called with the current position on each whole second.

See `mpv-run-time-update-functions'.")

;;; Internal Functions

;;; APIs

(defun mpv-update-video-info (&rest plist)
  "Record PLIST as the metadata of the file mpv is playing.

Called from the Python side once the file has finished loading."
  (setq mpv--video-info plist))

(defun mpv-show-box (text x y)
  "Draw an overlay box containing TEXT near the OSD position X, Y.

X and Y are OSD coordinates: the box is placed above them, so they
are the point the mouse was over."
  (websocket-bridge-call "mpv" "show-box" text x y))

(defun mpv-video-info ()
  "Ask the player to print the details of the current file."
  (websocket-bridge-call "mpv" "video-info"))

(defun mpv-definition-word (sub-text word x y)
  "Explain WORD near X, Y.

The explanation is whatever `mpv-definition-function' returns.  The
first argument is the whole subtitle line the word came from; it is
unused here and only keeps the argument order the Python side sends."
  (mpv-show-box
   (format "%s:\n %s" word
           (if (functionp mpv-definition-function)
               (funcall mpv-definition-function sub-text word x y)
             "You should customize `mpv-definition-function'."))
   x y))

(defun mpv-explain-text (sub-text word x y)
  "Show SUB-TEXT and WORD near X, Y.

The explanation is whatever `mpv-translation-function' returns for
SUB-TEXT."
  (mpv-show-box
   (format "%s:\n%s" sub-text
           (if (functionp mpv-translation-function)
               (funcall mpv-translation-function sub-text word x y)
             "You should customize `mpv-translation-function'."))
   x y))

(defun mpv-download-subtitle ()
  "Ask the Python side to download a subtitle for the current played file."
  (interactive)
  (websocket-bridge-call
   "mpv" "download-subtitle"
   (plist-get mpv--video-info "path" #'equal)))

(defun mpv-download-subtitle-select (&optional file)
  "Ask the Python side to download a subtitle for FILE."
  (interactive)
  (websocket-bridge-call
   "mpv" "download-subtitle"
   (or file
       (dired-get-filename nil t)
       (read-file-name "Download subtitle for file: "))))

(defun mpv-run-time-update-functions (time)
  "Call every registered hook with the playback position TIME.

See `mpv--time-update-functions'."
  (dolist (func mpv--time-update-functions)
    (funcall func time)))

;;; Interactive Functions

(defun mpv-start ()
  "Start the websocket bridge and launch the player."
  (interactive)
  (websocket-bridge-server-start)
  (websocket-bridge-app-start
   "mpv"
   mpv-python
   mpv-py-path))

(defun mpv-stop ()
  "Shut the player down."
  (interactive)
  (websocket-bridge-app-exit "mpv"))

(defun mpv-plause ()
  "Pause playback."
  (interactive)
  (websocket-bridge-call "mpv" "plause"))

(defun mpv-resume ()
  "Resume playback."
  (interactive)
  (websocket-bridge-call "mpv" "resume"))

(defun mpv-play (&optional file)
  "Play FILE, replacing whatever is playing now."
  (interactive)
  (websocket-bridge-call
   "mpv" "play"
   (or file
       (dired-get-filename nil t)
       (read-file-name "mpv play file: "))))

(defun mpv-restart ()
  "Restart the player and show its output buffer."
  (interactive)
  (mpv-stop)
  (mpv-start)
  (split-window-below -10)
  (other-window 1)
  (websocket-bridge-app-open-buffer "mpv"))

(provide 'mpv)
;;; mpv.el ends here
