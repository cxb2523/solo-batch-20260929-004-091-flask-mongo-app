# MODULE IMPORTS

# Flask modules
from flask import (Flask, render_template, request, url_for, redirect, abort,
                   jsonify)
from flask_login import (LoginManager, login_user, logout_user, login_required,
                         current_user)
from flask_talisman import Talisman
from flask_bcrypt import Bcrypt
from flask_wtf.csrf import CSRFProtect

# Other modules
from urllib.parse import urlparse, urljoin
import configparser
import os

# Local imports
from user import User, Anonymous
from message import Message
from note import Note
from email_utility import (send_registration_email, send_message_email)
from verification import confirm_token
from repositories import RepositoryManager


def create_app(repository_manager=None, config_overrides=None):
    """Application factory.

    ``repository_manager`` is injected here (dependency injection); when not
    supplied a manager is built from the ``MONGO_URI`` environment.
    """

    app = Flask(__name__)

    # Configuration
    config = configparser.ConfigParser()
    config.read(os.path.join(os.path.dirname(__file__), 'configuration.ini'))
    default = config['DEFAULT']
    app.secret_key = default['SECRET_KEY']
    app.config['PREFERRED_URL_SCHEME'] = "https"
    if config_overrides:
        app.config.update(config_overrides)

    # Inject repositories
    repos = repository_manager or RepositoryManager()
    app.repositories = repos

    # Create Bcrypt
    bc = Bcrypt(app)

    # Create Talisman (tests use plain HTTP)
    talisman = Talisman(
        app,
        content_security_policy={
            'default-src': [
                '\'self\'',
                'https://stackpath.bootstrapcdn.com',
                'https://pro.fontawesome.com',
                'https://code.jquery.com',
                'https://cdnjs.cloudflare.com'
            ]
        },
        force_https=not app.config.get("TESTING", False),
    )

    # Create CSRF protect
    csrf = CSRFProtect()
    csrf.init_app(app)

    # Create login manager
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.anonymous_user = Anonymous
    login_manager.login_view = "login"

    def status_payload():
        return repos.status()

    # Make backend status available to every template without changing any
    # existing render_template(...) signature.
    @app.context_processor
    def inject_backend_status():
        return {"backend_status": status_payload()}

    # ------------------------------------------------------------------
    # ROUTES
    # ------------------------------------------------------------------

    # Index
    @app.route('/')
    def index():
        return render_template('index.html')

    # Login
    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'GET':
            if current_user.is_authenticated:
                # Redirect to index if already authenticated
                return redirect(url_for('index'))
            # Render login page
            return render_template('login.html',
                                   error=request.args.get("error"))
        # Retrieve user from database
        user_data = repos.users.find_by_email(request.form['email'])
        if user_data:
            # Check password hash
            if bc.check_password_hash(user_data['password'],
                                      request.form['pass']):
                # Create user object to login (note password hash not stored
                # in session)
                user = User.make_from_dict(user_data)
                login_user(user)

                # Check for next argument (direct user to protected page
                # they wanted)
                next_url = request.args.get('next')
                if not is_safe_url(next_url):
                    return abort(400)

                # Go to profile page after login
                return redirect(next_url or url_for('profile'))

        # Redirect to login page on error
        return redirect(url_for('login', error=1))

    # Register
    @app.route('/register', methods=['POST', 'GET'])
    def register():
        if request.method == 'POST':
            # Trim input data
            email = request.form['email'].strip()
            title = request.form['title'].strip()
            first_name = request.form['first_name'].strip()
            last_name = request.form['last_name'].strip()
            password = request.form['pass'].strip()

            # Check if email address already exists
            existing_user = repos.users.find_by_email(email)

            if existing_user is None:
                logout_user()
                # Hash password
                hashpass = bc.generate_password_hash(password).decode('utf-8')
                # Create user object (note password hash not stored in
                # session)
                new_user = User(title, first_name, last_name, email)
                # Create dictionary data to save to database
                user_data_to_save = new_user.dict()
                user_data_to_save['password'] = hashpass

                # Insert user record to database
                if repos.users.add(user_data_to_save):
                    login_user(new_user)
                    send_registration_email(new_user)
                    return redirect(url_for('profile'))
                else:
                    # Handle database error
                    return redirect(url_for('register', error=2))

            # Handle duplicate email
            return redirect(url_for('register', error=1))

        # Return template for registration page if GET request
        return render_template('register.html',
                               error=request.args.get("error"))

    # Confirm email
    @app.route('/confirm/<token>', methods=['GET'])
    def confirm_email(token):
        logout_user()
        try:
            email = confirm_token(token)
            if email:
                if repos.users.mark_verified(email):
                    return render_template('confirm.html', success=True)
        except Exception:
            return render_template('confirm.html', success=False)
        else:
            return render_template('confirm.html', success=False)

    # Verification email
    @app.route('/verify', methods=['POST'])
    @login_required
    def send_verification_email():
        if current_user.verified == False:
            send_registration_email(current_user)
            return "Verification email sent"
        else:
            return "Your email address is already verified"

    # Profile
    @app.route('/profile', methods=['GET'])
    @login_required
    def profile():
        notes = repos.notes.find_active_by_user(current_user.id)
        return render_template('profile.html', notes=notes,
                               title=current_user.title)

    # Messages
    @app.route('/messages', methods=['GET'])
    @login_required
    def messages():
        all_users = repos.users.find_others(current_user.id)
        inbox_messages = repos.messages.find_inbox(current_user.id)
        sent_messages = repos.messages.find_sent(current_user.id)
        return render_template(
            'messages.html', users=all_users,
            inbox_messages=inbox_messages,
            sent_messages=sent_messages)

    # Logout
    @app.route('/logout', methods=['GET'])
    @login_required
    def logout():
        logout_user()
        return redirect(url_for('index'))

    # ------------------------------------------------------------------
    # POST REQUEST ROUTES
    # ------------------------------------------------------------------

    # Add note
    @app.route('/add_note', methods=['POST'])
    @login_required
    def add_note():
        title = request.form.get("title")
        body = request.form.get("body")
        user_id = current_user.id
        user_name = current_user.display_name()
        note = Note(title, body, user_id, user_name)
        success = repos.notes.add(note.dict())
        return jsonify({
            "success": success,
            "action": "add_note",
            "id": note.id,
            "title": title,
            "message": ("Success! Note added: " + title)
            if success else "Error! Could not add note",
            **status_payload(),
        })

    # Delete note
    @app.route('/delete_note', methods=['POST'])
    @login_required
    def delete_note():
        note_id = request.form.get("note_id")
        deleted = repos.notes.soft_delete(note_id)
        return jsonify({
            "success": deleted,
            "action": "delete_note",
            "id": note_id,
            "message": ("Success! Note deleted"
                        if deleted else "Note not found or already deleted"),
            **status_payload(),
        })

    # Send message
    @app.route('/send_message', methods=['POST'])
    @login_required
    def send_message():
        title = request.form.get("title")
        body = request.form.get("body")
        from_id = current_user.id
        from_name = current_user.display_name()
        to_id = request.form.get("user")
        to_user_dict = repos.users.find_by_id(to_id)
        to_user = User.make_from_dict(to_user_dict)
        to_name = to_user.display_name()
        message = Message(title, body, from_id, from_name, to_id, to_name)
        success = repos.messages.add(message.dict())
        if success:
            send_message_email(from_user=current_user, to_user=to_user,
                               message=message)
        return jsonify({
            "success": success,
            "action": "send_message",
            "id": message.id,
            "title": title,
            "to_name": to_name,
            "message": ("Success! Message sent to " + to_name + ": " + title)
            if success else "Error! Could not send message",
            **status_payload(),
        })

    # Delete message
    @app.route('/delete_message', methods=['POST'])
    @login_required
    def delete_message():
        message_id = request.form.get("message_id")
        deleted = repos.messages.soft_delete(message_id)
        return jsonify({
            "success": deleted,
            "action": "delete_message",
            "id": message_id,
            "message": ("Success! Message deleted"
                        if deleted
                        else "Message not found or already deleted"),
            **status_payload(),
        })

    # Hide sent message
    @app.route('/hide_sent_message', methods=['POST'])
    @login_required
    def hide_sent_message():
        message_id = request.form.get("message_id")
        hidden = repos.messages.hide_from_sender(message_id)
        if hidden:
            return "Success! Message hidden from sender"
        else:
            return "Error! Could not hide message"

    # Change Name
    @app.route('/change_name', methods=['POST'])
    @login_required
    def change_name():
        title = request.form['title'].strip()
        first_name = request.form['first_name'].strip()
        last_name = request.form['last_name'].strip()

        if repos.users.update_name(current_user.email, title, first_name,
                                   last_name):
            return "User name updated successfully"
        else:
            return "Error! Could not update user name"

    # Delete Account
    @app.route('/delete_account', methods=['POST'])
    @login_required
    def delete_account():
        user_id = current_user.id

        # Delete user details
        user_deleted = repos.users.delete(user_id)
        if user_deleted:
            logout_user()

        # Delete notes
        notes_deleted = repos.notes.delete_all_for_user(user_id) > 0

        # Delete messages
        messages_deleted = repos.messages.delete_all_for_user(user_id) > 0

        return {"user_deleted": user_deleted,
                "notes_deleted": notes_deleted,
                "messages_deleted": messages_deleted}

    # ------------------------------------------------------------------
    # Public status-bar demo API (CSRF exempt for easy curl / fetch use)
    # ------------------------------------------------------------------

    @app.route('/api/status', methods=['GET'])
    @csrf.exempt
    def api_status():
        return jsonify(status_payload())

    @app.route('/api/add_note', methods=['POST'])
    @csrf.exempt
    def api_add_note():
        note = Note("Demo note", "Created from the status bar",
                    "demo-user", "Demo User")
        success = repos.notes.add(note.dict())
        return jsonify({
            "success": success,
            "action": "add_note",
            "id": note.id,
            "message": ("Success! Note added: " + note.title)
            if success else "Error! Could not add note",
            **status_payload(),
        })

    @app.route('/api/delete_note', methods=['POST'])
    @csrf.exempt
    def api_delete_note():
        note_id = (request.form.get("note_id")
                   if request.form else None)
        if not note_id:
            note_id = repos.notes.latest_active_id()
        deleted = repos.notes.soft_delete(note_id) if note_id else False
        return jsonify({
            "success": deleted,
            "action": "delete_note",
            "id": note_id,
            "message": ("Success! Note deleted"
                        if deleted else "No note available to delete"),
            **status_payload(),
        })

    @app.route('/api/add_message', methods=['POST'])
    @csrf.exempt
    def api_add_message():
        message = Message("Demo message",
                          "Created from the status bar",
                          "demo-user", "Demo User",
                          "demo-user", "Demo User")
        success = repos.messages.add(message.dict())
        return jsonify({
            "success": success,
            "action": "add_message",
            "id": message.id,
            "message": ("Success! Message added"
                        if success else "Error! Could not add message"),
            **status_payload(),
        })

    @app.route('/api/delete_message', methods=['POST'])
    @csrf.exempt
    def api_delete_message():
        message_id = (request.form.get("message_id")
                      if request.form else None)
        if not message_id:
            message_id = repos.messages.latest_active_id()
        deleted = (repos.messages.soft_delete(message_id)
                   if message_id else False)
        return jsonify({
            "success": deleted,
            "action": "delete_message",
            "id": message_id,
            "message": ("Success! Message deleted"
                        if deleted else "No message available to delete"),
            **status_payload(),
        })

    # ------------------------------------------------------------------
    # LOGIN MANAGER REQUIREMENTS
    # ------------------------------------------------------------------

    # Load user from user ID
    @login_manager.user_loader
    def load_user(userid):
        # Return user object or none
        user = repos.users.find_by_id(userid)
        if user:
            return User.make_from_dict(user)
        return None

    # Safe URL
    def is_safe_url(target):
        ref_url = urlparse(request.host_url)
        test_url = urlparse(urljoin(request.host_url, target))
        return test_url.scheme in ('http', 'https') and \
            ref_url.netloc == test_url.netloc

    return app


# Heroku / direct execution
app = create_app()


if __name__ == '__main__':
    if os.environ.get('APP_LOCATION') == 'heroku':
        port = int(os.environ.get("PORT", 5000))
        app.run(host="0.0.0.0", port=port)
    else:
        app.run(host='localhost', port=8080, debug=True)
