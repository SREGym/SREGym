package main
import (
	"fmt"
	"net"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"github.com/charmbracelet/bubbles/textinput"
	"github.com/charmbracelet/bubbles/list"
)

//
// STYLES
//

type Styles struct {
	Header      lipgloss.Style
	InputField  lipgloss.Style
	Footer      lipgloss.Style
	Panel       lipgloss.Style
	Selected    lipgloss.Style
	Unselected  lipgloss.Style
}

func DefaultStyles() *Styles {
	glowColor := lipgloss.Color("99") // neon purple glow

	return &Styles{
		Header: lipgloss.NewStyle().
			Bold(true).
			Foreground(glowColor).
			Padding(0, 1),

		InputField: lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			BorderForeground(glowColor).
			Padding(1).
			Width(60),

		Footer: lipgloss.NewStyle().
			Foreground(lipgloss.Color("241")).
			Padding(0, 1),

		Panel: lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			BorderForeground(glowColor).
			Padding(1).
			Margin(1),

		Selected: lipgloss.NewStyle().
			Foreground(glowColor).
			Bold(true),

		Unselected: lipgloss.NewStyle().
			Foreground(lipgloss.Color("252")),
	}
}

//
// LIST ITEM
//

type appItem struct {
	title       string
	description string
}

func (a appItem) Title() string       { return a.title }
func (a appItem) Description() string { return a.description }
func (a appItem) FilterValue() string { return a.title }

//
// MODEL
//

type model struct {
	list     list.Model
	selected string
	width    int
	height   int
	answer   textinput.Model
	mode     string
	styles   *Styles
}

func New() *model {
	styles := DefaultStyles()

	delegate := list.NewDefaultDelegate()
	delegate.SetHeight(1)
	delegate.ShowDescription = false
	delegate.SetSpacing(0)

	items := []list.Item{
		appItem{"FleetCast", ""},
		appItem{"Hotel Reservation", ""},
		appItem{"Social Network", ""},
	}

	l := list.New(items, delegate, 0, 0)
	l.SetShowStatusBar(false)
	l.SetFilteringEnabled(false)
	l.SetShowHelp(false)
	l.Title = ""

	answer := textinput.New()
	answer.Placeholder = "Inject logs here..."
	answer.Focus()

	return &model{
		list:   l,
		answer: answer,
		mode:   "select",
		styles: styles,
	}
}

//
// INIT
//

func (m model) Init() tea.Cmd {
	return nil
}

//
// UPDATE
//

func (m model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {

	switch msg := msg.(type) {

	case tea.WindowSizeMsg:
		m.width = msg.Width
		m.height = msg.Height
		m.list.SetSize(msg.Width-6, msg.Height-8)
		return m, nil

	case tea.KeyMsg:

		switch m.mode {

		case "select":

			switch msg.String() {

			case "enter":
				m.selected = m.list.SelectedItem().(appItem).Title()
				m.mode = "inject"
				return m, nil

			case "ctrl+c", "q":
				return m, tea.Quit
			}

			var cmd tea.Cmd
			m.list, cmd = m.list.Update(msg)
			return m, cmd

		case "inject":

			switch msg.String() {

			case "enter":
				injectIntoSocket(m.selected, m.answer.Value())
				m.answer.SetValue("")
				return m, nil

			case "esc":
				m.mode = "select"
				return m, nil

			case "ctrl+c", "q":
				return m, tea.Quit
			}

			var cmd tea.Cmd
			m.answer, cmd = m.answer.Update(msg)
			return m, cmd
		}
	}

	return m, nil
}

//
// VIEW
//

func (m model) View() string {

	if m.width == 0 {
		return "Loading..."
	}

	header := m.styles.Header.Render("INJECT LOGS")

	footer := m.styles.Footer.Render(
		"↑/↓ Navigate  •  Enter Select  •  Esc Back  •  q Quit",
	)

	switch m.mode {

	case "select":

		// Custom render selection without purple square
		items := ""
		for i, item := range m.list.Items() {
			app := item.(appItem)
			if i == m.list.Index() {
				items += m.styles.Selected.Render(app.title) + "\n"
			} else {
				items += m.styles.Unselected.Render(app.title) + "\n"
			}
		}
		panelWidth := m.width -6
		panel := m.styles.Panel.Width(panelWidth).Render(items)

		return lipgloss.JoinVertical(
			lipgloss.Center,
			header,
			panel,
			footer,
		)

	case "inject":

		injectHeader := m.styles.Header.Render(
			fmt.Sprintf("Injecting into: %s", m.selected),
		)

		return lipgloss.JoinVertical(
			lipgloss.Center,
			injectHeader,
			"",
			m.styles.InputField.Render(m.answer.View()),
			"",
			footer,
		)
	}

	return ""
}

//
// SOCKET INJECTION
//

func injectIntoSocket(app string, payload string) {
	conn, err := net.Dial("unix", "/tmp/log_inject.sock")
	if err != nil {
		fmt.Println("Socket error:", err)
		return
	}
	defer conn.Close()

	conn.Write([]byte(payload))
}

//
// MAIN
//

func main() {
	m := New()
	p := tea.NewProgram(m, tea.WithAltScreen())
	if err := p.Start(); err != nil {
		panic(err)
	}
}